import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { VisualSocketClient } from '../lib/wsClient'
import { GameBridge } from '../game/GameBridge'
import { useBoardStore, type BoardStoreState } from '../kanban/BoardStore'
import { useChatStore, type ChatStoreState } from '../chat/ChatStore'
import { useSessionStore, type SessionStoreState } from '../stores/SessionStore'
import { useProjectStore, type ProjectStoreState } from '../stores/ProjectStore'
import { mapCollabSyncPayload, mapBackendMessage, mapBackendChannel, mapBackendSession, mapBackendBoard, mapBackendColumn, mapBackendTask, mergeSessionDetailHasMore } from '../lib/collabSync'
import { normalizeOrgInfoPayload } from '../lib/runtimeOrg'
import { companyRuntimeControlPatchForBoardStatus } from '../lib/sessionRuntime'
import { extractSessionRecruitmentByRole, sessionChannelId } from '../lib/sessionRecruitment'
import { resolveCanonicalTurnId, terminalAssistantTurnId } from '../lib/turnIdentity'
import { unassignAgent } from '../game/map/OfficeStore'
import { t } from '../lib/locale'
import {
  normalizeCompanyProfile, normalizeExecMode, companyProfileForExecMode, orgIdForExecMode,
  normalizeTaskPreferredAgent, mapAgentListPayload, hasOwnPayloadField, runtimeStatusClearsDisplayTool,
  workItemIdentityPatchFromPayload, sessionRuntimePatchFromPayload, kanbanRuntimePatchFromPayload, shouldRefreshLiveSession,
} from '../lib/appUtils'
import { MAX_LOG_ITEMS, TASK_MODE_LOW_VALUE_RUNTIME_EVENTS, SESSION_DETAIL_REFRESH_LOW_VALUE_RUNTIME_EVENTS } from '../types/app'
import type { AppExecMode } from '../types/app'
import type { AgentInfo, EmployeeDetailPayload, OrgCreateMemberInput, OrgSavedCreatePayload, OrgInfoPayload, ReorgProposalInfo, SavedOrgSummary, SocketStatus, TalentTemplate, VisualEvent, VisualSnapshot } from '../types/visual'
import type { KanbanTask, TaskPreferredAgent } from '../types/kanban'

export function useAppWebSocket(bridgeRef: React.MutableRefObject<GameBridge>) {
  const clientRef = useRef<VisualSocketClient | null>(null)
  const [wsUrl, setWsUrl] = useState(() => {
    const wsProto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${wsProto}://${window.location.hostname}:${window.location.port || '8765'}/ws`
  })
  const [wsUrlInput, setWsUrlInput] = useState(wsUrl)
  const [status, setStatus] = useState<SocketStatus>('disconnected')
  const [statusDetail, setStatusDetail] = useState('')
  const [snapshot, setSnapshot] = useState<VisualSnapshot | null>(null)
  const [events, setEvents] = useState<VisualEvent[]>([])
  const [uiTick, setUiTick] = useState(0)
  const [swarmAgents, setSwarmAgents] = useState<AgentInfo[]>([])
  const [lastTaskDoneAgent, setLastTaskDoneAgent] = useState<string | null>(null)
  const [globalExecMode, setGlobalExecMode] = useState<AppExecMode>('task')
  const [globalCompanyProfile, setGlobalCompanyProfile] = useState<'corporate' | 'custom'>('corporate')
  const [globalTaskPreferredAgent, setGlobalTaskPreferredAgent] = useState<TaskPreferredAgent>('native')
  const [orgInfoData, setOrgInfoData] = useState<OrgInfoPayload | null>(null)
  const [commsState, setCommsState] = useState<import('../lib/wsClient').CommsStatePayload | null>(null)
  const [commsMessage, setCommsMessage] = useState<import('../lib/wsClient').CommsMessagePayload | null>(null)
  const [talentTemplates, setTalentTemplates] = useState<TalentTemplate[]>([])
  const [employeeDetail, setEmployeeDetail] = useState<EmployeeDetailPayload | null>(null)
  const [reorgProposals, setReorgProposals] = useState<ReorgProposalInfo[]>([])
  const [marketPresets, setMarketPresets] = useState<any[]>([])
  const [marketPreviewData, setMarketPreviewData] = useState<any>(null)
  const [configExportYaml, setConfigExportYaml] = useState<string | null>(null)
  const [configImportPreview, setConfigImportPreview] = useState<{ roles_added: number; roles_removed: number; employees_changed: number } | null>(null)
  const [configImportError, setConfigImportError] = useState<string | null>(null)
  const [savedOrgsList, setSavedOrgsList] = useState<SavedOrgSummary[] | null>(null)
  const [activeSavedOrg, setActiveSavedOrg] = useState<string | null>(null)
  const [savedOrgVersionAtLoad, setSavedOrgVersionAtLoad] = useState<number | null>(null)
  const [orgCreatePending, setOrgCreatePending] = useState(false)
  const [orgCreateResult, setOrgCreateResult] = useState<(OrgSavedCreatePayload & { nonce: number }) | null>(null)
  const [orgToast, setOrgToast] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)
  const [hiringTemplateId, setHiringTemplateId] = useState<string | null>(null)
  const [toastMessage, setToastMessage] = useState<string | null>(null)
  const [toastType, setToastType] = useState<'success' | 'error'>('success')
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null)
  const [deletingAgentId, setDeletingAgentId] = useState<string | null>(null)
  const [executionPanelTaskId, setExecutionPanelTaskId] = useState<string | null>(null)

  const timersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())
  const replayedEventIds = useRef<Set<string>>(new Set())
  const swarmAgentsRef = useRef<AgentInfo[]>([])
  const globalExecModeRef = useRef<AppExecMode>('task')
  const kanbanCreateRef = useRef<((data: { title: string; description?: string; priority: null; assignee_id?: string }) => void) | null>(null)
  const chatStoreRef = useRef<ChatStoreState | null>(null)
  const boardStoreRef = useRef<BoardStoreState | null>(null)
  const sessionStoreRef = useRef<SessionStoreState | null>(null)
  const projectStoreRef = useRef<ProjectStoreState | null>(null)
  const activeProjectIdRef = useRef<string>('default')
  const pendingProjectSwitchRef = useRef<string | null>(null)
  const currentSwitchSeqRef = useRef<string>('')
  const projectViewGenerationRef = useRef<number>(0)
  const userSelectedProjectRef = useRef<boolean>(false)
  const projectsHydratedRef = useRef<boolean>(false)
  const lastProjectIndexRefreshRef = useRef<number>(0)
  const pendingSessionCreateRef = useRef(false)
  const pendingSessionCreateProjectIdRef = useRef<string | null>(null)
  const pendingSessionCreateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingSessionDetailRefreshRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const runtimeLogsAckHandlersRef = useRef<Set<(payload: Record<string, unknown>) => void>>(new Set())
  const pendingDeltaFlushRef = useRef<Map<string, { draftText: string; draftIteration?: number; draftTurnId?: string; sessionPatch: Partial<import('../types/kanban').Session>; kanbanPatch: Partial<KanbanTask> }>>(new Map())
  const deltaFlushTimerRef = useRef<number | null>(null)
  const uiTickTimerRef = useRef<number | null>(null)

  const boardStore = useBoardStore()
  const chatStore = useChatStore()
  const sessionStore = useSessionStore()
  const projectStore = useProjectStore()

  const normalizeProjectId = useCallback((value: unknown): string => {
    const projectId = typeof value === 'string' ? value.trim() : ''
    return projectId || 'default'
  }, [])
  const getActiveProjectId = useCallback(() => normalizeProjectId(activeProjectIdRef.current), [normalizeProjectId])
  const newSwitchSeq = useCallback(() => `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`, [])

  const projectIdFromPayload = useCallback((payload: Record<string, unknown> | null | undefined): string => {
    if (!payload || typeof payload !== 'object') return ''
    for (const key of ['project_id', 'projectId', 'active_project_id', 'activeProjectId']) {
      const value = payload[key]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
    const data = payload.data
    if (data && typeof data === 'object') {
      for (const key of ['project_id', 'projectId']) {
        const value = (data as Record<string, unknown>)[key]
        if (typeof value === 'string' && value.trim()) return value.trim()
      }
    }
    return ''
  }, [])

  const payloadMatchesActiveProject = useCallback((payload: Record<string, unknown> | null | undefined, allowMissing = false): boolean => {
    const projectId = projectIdFromPayload(payload)
    if (!projectId) return allowMissing
    return projectId === getActiveProjectId()
  }, [getActiveProjectId, projectIdFromPayload])

  const payloadViewGeneration = useCallback((payload: Record<string, unknown> | null | undefined): number | null => {
    if (!payload || typeof payload !== 'object') return null
    const value = payload.view_generation ?? payload.viewGeneration
    if (typeof value === 'number') return value
    if (typeof value === 'string' && value.trim()) { const p = Number(value); return Number.isFinite(p) ? p : null }
    return null
  }, [])

  const payloadMatchesCurrentSwitch = useCallback((payload: Record<string, unknown> | null | undefined): boolean => {
    const projectId = projectIdFromPayload(payload)
    if (!projectId) return false
    const pendingProjectId = pendingProjectSwitchRef.current
    if (projectId !== getActiveProjectId() && projectId !== pendingProjectId) return false
    const generation = payloadViewGeneration(payload)
    if (generation !== null && generation !== projectViewGenerationRef.current) return false
    const seq = typeof (payload?.switch_seq ?? payload?.switchSeq) === 'string' ? String(payload?.switch_seq ?? payload?.switchSeq).trim() : ''
    return !seq || !currentSwitchSeqRef.current || seq === currentSwitchSeqRef.current
  }, [getActiveProjectId, payloadViewGeneration, projectIdFromPayload])

  const shouldSuppressTaskNotFound = useCallback((payload: Record<string, unknown> | null | undefined): boolean => {
    if (!payload || payload.error !== 'task_not_found') return false
    if (!payloadMatchesActiveProject(payload, false)) return true
    if (typeof payload.action === 'string' && payload.action !== 'session_detail') return false
    const generation = payloadViewGeneration(payload)
    if (generation !== null && generation !== projectViewGenerationRef.current) return true
    const taskId = typeof payload.task_id === 'string' ? payload.task_id : ''
    if (!taskId) return true
    return !(sessionStoreRef.current?.sessions ?? []).some(session => session.taskId === taskId)
  }, [payloadMatchesActiveProject, payloadViewGeneration])

  const clearPendingSessionDetailRefreshes = useCallback(() => {
    for (const tid of pendingSessionDetailRefreshRef.current.values()) clearTimeout(tid)
    pendingSessionDetailRefreshRef.current.clear()
  }, [])

  const scheduleSessionDetailRefresh = useCallback((taskId: string, detailLevel: 'summary' | 'full' = 'full', force = false, projectId?: string) => {
    if (!taskId) return
    const scopedProjectId = projectId || getActiveProjectId()
    const generation = projectViewGenerationRef.current
    if (!force && !shouldRefreshLiveSession(taskId, sessionStoreRef.current)) return
    const timerKey = `${scopedProjectId}:${generation}:${taskId}`
    const existing = pendingSessionDetailRefreshRef.current.get(timerKey)
    if (existing) { clearTimeout(existing); pendingSessionDetailRefreshRef.current.delete(timerKey) }
    const tid = setTimeout(() => {
      pendingSessionDetailRefreshRef.current.delete(timerKey)
      if (scopedProjectId !== getActiveProjectId()) return
      if (generation !== projectViewGenerationRef.current) return
      if (!force && !shouldRefreshLiveSession(taskId, sessionStoreRef.current)) return
      const client = clientRef.current
      sessionStoreRef.current?.updateSession(taskId, { detailLoading: true, detailError: undefined, viewGeneration: generation })
      if (!client) { sessionStoreRef.current?.updateSession(taskId, { detailLoading: false, detailError: 'connection_unavailable' }); return }
      void client.sessionDetail(scopedProjectId, taskId, {
        limit: 200, detailLevel,
        include: detailLevel === 'full' ? ['messages', 'session_state', 'progress', 'work_items', 'runtime_context'] : ['messages', 'session_state'],
        viewGeneration: generation,
      }).then((payload) => {
        if (payload.ok !== false) return
        if (scopedProjectId !== getActiveProjectId()) return
        if (generation !== projectViewGenerationRef.current) return
        sessionStoreRef.current?.updateSession(taskId, { detailLoading: false, detailError: String(payload.error ?? 'request_failed') })
      })
    }, 180)
    pendingSessionDetailRefreshRef.current.set(timerKey, tid)
  }, [getActiveProjectId])

  const showToast = useCallback((msg: string, type: 'success' | 'error' = 'success') => {
    setToastMessage(msg); setToastType(type)
    setTimeout(() => setToastMessage(null), 3000)
  }, [])

  const clearPendingSessionCreate = useCallback(() => {
    if (pendingSessionCreateTimerRef.current) { clearTimeout(pendingSessionCreateTimerRef.current); pendingSessionCreateTimerRef.current = null }
    pendingSessionCreateRef.current = false
    pendingSessionCreateProjectIdRef.current = null
  }, [])

  const beginPendingSessionCreate = useCallback((projectId: string) => {
    clearPendingSessionCreate()
    pendingSessionCreateRef.current = true
    pendingSessionCreateProjectIdRef.current = projectId
    pendingSessionCreateTimerRef.current = setTimeout(() => {
      if (!pendingSessionCreateRef.current || pendingSessionCreateProjectIdRef.current !== projectId) return
      clearPendingSessionCreate()
      setStatusDetail('create_session_timeout')
      showToast(t('app.sessionTimeout'), 'error')
    }, 30_000)
  }, [clearPendingSessionCreate, showToast])

  const beginProjectSwitch = useCallback((projectId: string): string => {
    const nextProjectId = normalizeProjectId(projectId)
    const switchSeq = newSwitchSeq()
    userSelectedProjectRef.current = true
    currentSwitchSeqRef.current = switchSeq
    projectViewGenerationRef.current += 1
    pendingProjectSwitchRef.current = nextProjectId
    clearPendingSessionDetailRefreshes()
    setStatusDetail(`Switching to ${nextProjectId}...`)
    return switchSeq
  }, [clearPendingSessionDetailRefreshes, newSwitchSeq, normalizeProjectId])

  const flushPendingDeltas = useCallback((onlyTaskId?: string) => {
    const pending = pendingDeltaFlushRef.current
    if (pending.size === 0) return
    const ids = onlyTaskId ? [onlyTaskId] : Array.from(pending.keys())
    for (const taskId of ids) {
      const entry = pending.get(taskId)
      if (!entry) continue
      pending.delete(taskId)
      const ss = sessionStoreRef.current
      if (entry.draftText) ss?.appendDraft(taskId, entry.draftText, entry.draftIteration, entry.draftTurnId)
      ss?.updateSession(taskId, entry.sessionPatch)
      if (Object.keys(entry.kanbanPatch).length > 0) boardStoreRef.current?.updateTask(taskId, entry.kanbanPatch)
    }
    if (pending.size === 0 && deltaFlushTimerRef.current !== null) { window.clearTimeout(deltaFlushTimerRef.current); deltaFlushTimerRef.current = null }
  }, [])

  const scheduleDeltaFlush = useCallback(() => {
    if (deltaFlushTimerRef.current !== null) return
    deltaFlushTimerRef.current = window.setTimeout(() => { deltaFlushTimerRef.current = null; flushPendingDeltas() }, 80)
  }, [flushPendingDeltas])

  const bumpUiTickThrottled = useCallback(() => {
    if (uiTickTimerRef.current !== null) return
    uiTickTimerRef.current = window.setTimeout(() => { uiTickTimerRef.current = null; setUiTick(n => n + 1) }, 300)
  }, [])

  // Sync store refs
  useEffect(() => {
    chatStoreRef.current = chatStore
    boardStoreRef.current = boardStore
    sessionStoreRef.current = sessionStore
    projectStoreRef.current = projectStore
    kanbanCreateRef.current = (data) => {
      if (!boardStore.activeBoardId || boardStore.activeBoardColumns.length === 0) return
      const todoCol = boardStore.activeBoardColumns.find(c => c.name === 'Todo')
      const colId = todoCol?.id ?? boardStore.activeBoardColumns[0].id
      boardStore.createTask({ boardId: boardStore.activeBoardId, columnId: colId, title: data.title, priority: data.priority, assigneeIds: data.assignee_id ? [data.assignee_id] : [] })
    }
  })
  useEffect(() => { swarmAgentsRef.current = swarmAgents }, [swarmAgents])
  useEffect(() => { globalExecModeRef.current = globalExecMode }, [globalExecMode])

  // Per-session recruitment for org canvas
  const activeSessionId = sessionStore.activeSessionId
  const sessionRecruitmentByRole = useMemo(() => {
    if (!activeSessionId) return null
    return extractSessionRecruitmentByRole(chatStore.getChannelMessages(sessionChannelId(activeSessionId)))
  }, [activeSessionId, chatStore])

  // ── Main WebSocket connection effect ──
  useEffect(() => {
    const client = new VisualSocketClient(wsUrl, {
      onSnapshot: (data) => {
        if (!payloadMatchesCurrentSwitch(data as unknown as Record<string, unknown>)) return
        setSnapshot(data)
        const timeline = data.timeline.slice(-MAX_LOG_ITEMS)
        setEvents(timeline)
        const ids = new Set<string>()
        for (const evt of timeline) ids.add(evt.event_id)
        replayedEventIds.current = ids
        bridgeRef.current.pushSnapshot(data)
        const agentEntries = Object.entries(data.agents ?? {})
        if (agentEntries.length > 0) {
          const infos = mapAgentListPayload(agentEntries.map(([id, info]) => ({ ...((info && typeof info === 'object') ? info as Record<string, unknown> : {}), agent_id: id })), swarmAgentsRef.current)
          swarmAgentsRef.current = infos
          setSwarmAgents(infos)
        }
        if (data.exec_mode) setGlobalExecMode(normalizeExecMode(data.exec_mode))
        if (data.company_profile) setGlobalCompanyProfile(normalizeCompanyProfile(data.company_profile))
        if (data.task_preferred_agent) setGlobalTaskPreferredAgent(normalizeTaskPreferredAgent(data.task_preferred_agent))
        setUiTick((n) => n + 1)
      },
      onEvent: (evt) => {
        try {
          if (!payloadMatchesActiveProject(evt as unknown as Record<string, unknown>, false)) return
          if (replayedEventIds.current.has(evt.event_id)) { replayedEventIds.current.delete(evt.event_id); return }
          setEvents((prev) => [...prev.slice(-MAX_LOG_ITEMS + 1), evt])
          bridgeRef.current.pushEvent(evt)
          if (evt.type === 'task_routed' && evt.agent_id) {
            if (globalExecModeRef.current === 'company' || globalExecModeRef.current === 'org') return
            const taskId = evt.data?.task_id as string | undefined
            if (taskId && !boardStoreRef.current?.tasks.find(t => t.id === taskId)) {
              const preview = typeof evt.data?.content_preview === 'string' ? evt.data.content_preview.slice(0, 80) : t('app.task')
              kanbanCreateRef.current?.({ title: preview, priority: null, assignee_id: evt.agent_id })
            }
          }
          if (evt.type === 'task_done' && evt.agent_id) setLastTaskDoneAgent(evt.agent_id)
          if (['turn_started','assistant_delta','thinking_delta','status_snapshot','tool_started','tool_progress','tool_completed','permission_requested','permission_resolved','cost_update','context_usage','context_warning','subagent_started','subagent_updated','subagent_completed','member_inbox_updated','compaction_applied','checkpoint_saved','turn_completed','turn_failed'].includes(evt.type)) {
            const data = evt.data as Record<string, unknown>
            const taskId = typeof data.task_id === 'string' ? data.task_id : ''
            if (taskId) {
              const isDeltaEvent = evt.type === 'assistant_delta' || evt.type === 'thinking_delta'
              const bufferedDraftTurnId = pendingDeltaFlushRef.current.get(taskId)?.draftTurnId
              if (!isDeltaEvent) flushPendingDeltas(taskId)
              const ss = sessionStoreRef.current
              const bs = boardStoreRef.current
              const existingSession = ss?.sessions.find(session => session.taskId === taskId)
              const projectionId = typeof data.work_item_projection_id === 'string' ? data.work_item_projection_id : ''
              const executionMode = typeof data.execution_mode === 'string' ? data.execution_mode : ''
              const isTaskModeRuntime = executionMode === 'task_mode' || projectionId === 'task_mode_execution'
              const turnId = resolveCanonicalTurnId(data) || undefined
              const marksCompanyRuntime = !!projectionId && projectionId !== 'task_mode_execution' && !isTaskModeRuntime
              if (marksCompanyRuntime && !isDeltaEvent && existingSession?.isCompanyRuntime !== true) ss?.setCompanyRuntime(taskId, true)
              const runtimePartial: Partial<import('../types/kanban').Session> = {
                lastRuntimeEventType: evt.type,
                ...(evt.type === 'member_inbox_updated' ? {} : { updatedAt: Date.now() }),
                ...sessionRuntimePatchFromPayload(data),
              }
              const toolName = typeof data.tool_name === 'string' ? data.tool_name : undefined
              const kanbanPatch: Partial<KanbanTask> = {
                ...kanbanRuntimePatchFromPayload(data),
                ...(toolName !== undefined ? { currentTool: toolName || undefined } : {}),
                ...(toolName ? { displayTool: toolName } : {}),
              }
              if (isDeltaEvent) {
                const pending = pendingDeltaFlushRef.current
                let entry = pending.get(taskId)
                const deltaText = evt.type === 'assistant_delta' && typeof data.text === 'string' ? data.text : ''
                if (entry && deltaText && entry.draftText && entry.draftTurnId !== undefined && turnId !== undefined && entry.draftTurnId !== turnId) { flushPendingDeltas(taskId); entry = undefined }
                if (!entry) { entry = { draftText: '', sessionPatch: {}, kanbanPatch: {} }; pending.set(taskId, entry) }
                if (deltaText) {
                  entry.draftText += deltaText
                  entry.draftIteration = typeof data.iteration === 'number' ? data.iteration : entry.draftIteration ?? existingSession?.draftIteration
                  if (turnId !== undefined) entry.draftTurnId = turnId
                }
                entry.sessionPatch = { ...entry.sessionPatch, ...runtimePartial, ...(marksCompanyRuntime ? { isCompanyRuntime: true } : {}) }
                entry.kanbanPatch = { ...entry.kanbanPatch, ...kanbanPatch }
                scheduleDeltaFlush()
              } else {
                const activeDraftTurnId = String(bufferedDraftTurnId ?? existingSession?.draftTurnId ?? '').trim()
                const startsNewCanonicalTurn = evt.type === 'turn_started' && !!turnId && !!activeDraftTurnId && turnId !== activeDraftTurnId
                if (evt.type === 'turn_failed' || startsNewCanonicalTurn) ss?.clearDraft(taskId)
                ss?.updateSession(taskId, runtimePartial)
                bs?.updateTask(taskId, kanbanPatch)
              }
              const skipDetailRefresh = (isTaskModeRuntime && TASK_MODE_LOW_VALUE_RUNTIME_EVENTS.has(evt.type)) || SESSION_DETAIL_REFRESH_LOW_VALUE_RUNTIME_EVENTS.has(evt.type)
              if (evt.type !== 'assistant_delta' && !skipDetailRefresh) scheduleSessionDetailRefresh(taskId)
            }
          }
          if (evt.type === 'agent_removed' && evt.agent_id) {
            const agentId = evt.agent_id
            unassignAgent(agentId)
            const cs = chatStoreRef.current
            if (cs) { cs.markSenderDeleted(agentId); cs.removeParticipant(agentId) }
          }
          bumpUiTickThrottled()
        } catch (e) { console.error('[onEvent] Error:', e, evt) }
      },
      onAck: (payload) => {
        try {
          if (payload.action === 'runtime_logs' || 'runtime_events' in payload) { for (const handler of runtimeLogsAckHandlersRef.current) handler(payload); return }
          if (payload.ok === false) {
            if (payload.action === 'create_session') clearPendingSessionCreate()
            if (shouldSuppressTaskNotFound(payload)) return
            if (payload.action === 'session_detail' && typeof payload.task_id === 'string') sessionStoreRef.current?.updateSession(payload.task_id, { detailLoading: false, detailError: String(payload.error ?? 'request_failed') })
            setStatusDetail(String(payload.error ?? 'request_failed'))
            setDeletingAgentId(null); setConfirmDeleteId(null)
            showToast(String(payload.error ?? t('app.requestFailed')), 'error')
          }
          if (payload.ok && payload.action === 'employee_imported') showToast(t('app.employeeDeployed'))
          if (payload.ok && payload.action === 'talent_imported') showToast(t('app.importedTemplates', { n: payload.count ?? 0 }))
          if (payload.ok && payload.action === 'talent_hired') { setHiringTemplateId(null); showToast(t('app.hiredToast', { name: payload.name ?? t('composer.agent') })) }
          if (payload.ok && payload.action === 'architecture_reset') { setActiveSavedOrg(null); setSavedOrgVersionAtLoad(null) }
          if (payload.ok && payload.action === 'session_detail') {
            if (!payloadMatchesActiveProject(payload, false)) return
            const detailGeneration = payloadViewGeneration(payload)
            if (detailGeneration !== null && detailGeneration !== projectViewGenerationRef.current) return
            const detailTaskId = typeof payload.task_id === 'string' ? payload.task_id : ''
            const detailMessages = Array.isArray(payload.messages) ? payload.messages.map(mapBackendMessage) : []
            const totalMessageCount = typeof payload.message_count === 'number' ? payload.message_count : detailMessages.length
            const detailLevel = payload.detail_level === 'full' ? 'full' : 'summary'
            const cs = chatStoreRef.current
            if (cs && detailMessages.length > 0) cs.mergeMessagesFromBackend(detailMessages)
            const ss = sessionStoreRef.current
            if (ss && detailTaskId) {
              const existingSession = ss.sessions.find(session => session.taskId === detailTaskId)
              const previousHasMore = detailLevel === 'full' ? existingSession?.fullHasMore : existingSession?.summaryHasMore
              const detailHasMore = mergeSessionDetailHasMore(previousHasMore, payload.has_more === true, payload.client_history_page === true)
              const draftTurnId = String(existingSession?.draftTurnId ?? '').trim()
              const detailHasFinalForDraft = !!draftTurnId && !!cs && detailMessages.some(message => terminalAssistantTurnId(message) === draftTurnId)
              if (detailHasFinalForDraft) ss.clearDraft(detailTaskId)
              const rawSessionState = payload.session_state
              const sessionPatch = rawSessionState && typeof rawSessionState === 'object' ? mapBackendSession(rawSessionState) : null
              ss.updateSession(detailTaskId, {
                ...(sessionPatch ?? {}),
                ...(typeof payload.handoff_context === 'string' ? { handoffContext: payload.handoff_context } : {}),
                ...(typeof payload.handoff_to === 'string' ? { handoffTo: payload.handoff_to } : {}),
                messageCount: totalMessageCount, detailLoaded: true,
                ...(detailLevel === 'full' ? { fullLoaded: !detailHasMore } : {}),
                hasMore: detailHasMore,
                ...(detailLevel === 'full' ? { fullHasMore: detailHasMore } : { summaryHasMore: detailHasMore }),
                detailLoading: false, detailError: undefined,
                viewGeneration: detailGeneration ?? projectViewGenerationRef.current,
              })
            }
          }
          if (Array.isArray(payload.agents)) {
            const nextAgents = mapAgentListPayload(payload.agents as unknown[], swarmAgentsRef.current)
            swarmAgentsRef.current = nextAgents; setSwarmAgents(nextAgents)
            for (const agent of nextAgents) bridgeRef.current.ensureAgent(agent.agent_id, agent.name, agent.office_id, agent.appearance?.palette, agent.appearance?.desk_id)
            setUiTick((n) => n + 1)
          }
          if (payload.ok && (payload.agent_id || payload.deleted)) {
            if (payload.deleted) { setDeletingAgentId(null); setConfirmDeleteId(null); showToast(t('app.agentRemoved')) }
            if (payload.agent_id && !payload.deleted) showToast(t('app.agentCreated'))
            clientRef.current?.listAgents()
          }
          if (payload.ok && Array.isArray(payload.projects)) {
            const ps = projectStoreRef.current
            if (ps) {
              const previousActiveId = getActiveProjectId()
              const initialHydration = !projectsHydratedRef.current
              const shouldUseBackendActive = !projectsHydratedRef.current && !userSelectedProjectRef.current
              const backendActiveId = typeof payload.active_project_id === 'string' ? payload.active_project_id.trim() : ''
              const createdProjectId = payload.action === 'create_project' && typeof payload.project_id === 'string' ? payload.project_id.trim() : ''
              const activeId = shouldUseBackendActive ? normalizeProjectId(createdProjectId || backendActiveId || activeProjectIdRef.current) : (createdProjectId ? normalizeProjectId(createdProjectId) : getActiveProjectId())
              if (createdProjectId) userSelectedProjectRef.current = true
              projectsHydratedRef.current = true
              activeProjectIdRef.current = activeId
              ps.initFromBackend(payload.projects as { id: string; name: string }[], activeId)
              if (activeId !== previousActiveId || initialHydration) {
                const switchSeq = newSwitchSeq()
                currentSwitchSeqRef.current = switchSeq
                pendingProjectSwitchRef.current = activeId
                projectViewGenerationRef.current += 1
                setStatusDetail(`Switching to ${activeId}...`)
                clientRef.current?.switchProject(activeId, switchSeq)
              }
            }
          }
          if (payload.ok && Array.isArray(payload.channels)) {
            if (!payloadMatchesCurrentSwitch(payload)) return
            const syncData = mapCollabSyncPayload(payload)
            const syncProjectId = projectIdFromPayload(payload)
            if (!syncProjectId) return
            const applyingProjectSwitch = pendingProjectSwitchRef.current === syncProjectId || getActiveProjectId() !== syncProjectId
            activeProjectIdRef.current = syncProjectId
            pendingProjectSwitchRef.current = null
            projectStoreRef.current?.setActiveProject(syncProjectId)
            if (applyingProjectSwitch) { clearPendingSessionDetailRefreshes(); setExecutionPanelTaskId(null); setCommsState(null); setCommsMessage(null) }
            setStatusDetail('')
            const cs = chatStoreRef.current
            if (cs) cs.initFromBackend(syncProjectId, syncData.channels, syncData.messages)
            const bs = boardStoreRef.current
            if (bs) bs.initFromBackend(syncProjectId, syncData.boards, syncData.columns, syncData.tasks)
            const ss = sessionStoreRef.current
            if (ss) { ss.initFromBackend(syncProjectId, syncData.sessions); const activeId = ss.activeSessionId; if (activeId) scheduleSessionDetailRefresh(activeId, 'full', true) }
          }
        } catch (e) { console.error('[onAck] Error:', e) }
      },
      onStatus: (next, detail) => {
        setStatus(next); setStatusDetail(detail ?? '')
        if (next === 'connected') { const projectId = getActiveProjectId(); client.listProjects(); client.orgInfo(); client.orgSavedList(); client.collabSync(projectId, undefined, projectViewGenerationRef.current) }
      },
      onCollabMessage: (type, payload) => {
        try {
          if (type === 'project_index_push') { if (!payloadMatchesCurrentSwitch(payload)) return } else if (!payloadMatchesActiveProject(payload, false)) return
          const cs = chatStoreRef.current
          if (type === 'chat_new_message') { if (cs) cs.addMessageFromBackend(mapBackendMessage(payload)) }
          else if (type === 'chat_channel_created') { if (cs) cs.addChannelFromBackend(mapBackendChannel(payload)) }
          else if (type === 'session_runtime_control') {
            const ss = sessionStoreRef.current
            if (ss) {
              const taskIds = Array.isArray(payload.task_ids) ? payload.task_ids.map(String).filter(Boolean) : []
              const patch: Partial<import('../types/kanban').Session> = { runtimeControlState: String(payload.runtime_control_state ?? payload.runtimeControlState ?? 'idle') as any, canStop: Boolean(payload.can_stop ?? payload.canStop), canResume: Boolean(payload.can_resume ?? payload.canResume), resumeParentSessionId: String(payload.resume_parent_session_id ?? payload.resumeParentSessionId ?? ''), pendingRuntimeCheckpointId: String(payload.pending_runtime_checkpoint_id ?? payload.pendingRuntimeCheckpointId ?? ''), stopIntentId: String(payload.stop_intent_id ?? payload.stopIntentId ?? '') }
              for (const taskId of taskIds) ss.updateSession(taskId, patch)
            }
          } else if (type === 'board_task_status_changed') {
            const taskId = String(payload.task_id ?? ''); const columnId = String(payload.column_id ?? ''); const statusStr = String(payload.status ?? '')
            const isTerminal = ['done', 'failed', 'cancelled'].includes(statusStr)
            if (taskId && columnId) {
              const bs = boardStoreRef.current
              if (bs) { const task = bs.tasks.find(t => t.id === taskId); if (task) { const partial: Partial<import('../types/kanban').KanbanTask> = {}; if (task.columnId !== columnId) bs.moveTask(taskId, columnId, 0); if (statusStr === 'running') { if (!task.agentStatus || task.agentStatus === 'idle') partial.agentStatus = 'reflecting' } else if (task.agentStatus || task.currentTool || task.displayTool) { partial.agentStatus = undefined; partial.currentTool = undefined; partial.displayTool = undefined }; if (Object.keys(partial).length > 0) bs.updateTask(taskId, partial) } }
              const ss = sessionStoreRef.current
              if (ss) { const session = ss.sessions.find(s => s.taskId === taskId); const sessionPatch: Partial<import('../types/kanban').Session> = { columnId, status: statusStr || columnId }; if (statusStr === 'running') { if (!session?.agentStatus || session.agentStatus === 'idle') sessionPatch.agentStatus = 'reflecting' } else if (session?.agentStatus || session?.currentTool || session?.displayTool || isTerminal) { sessionPatch.agentStatus = undefined; sessionPatch.currentTool = undefined; sessionPatch.displayTool = undefined }; Object.assign(sessionPatch, companyRuntimeControlPatchForBoardStatus(session, statusStr)); ss.updateSession(taskId, { ...sessionPatch }) }
            }
          } else if (type === 'execution_mode_resolved') {
            const mode = String(payload.mode ?? ''); const profile = String(payload.profile ?? '')
            if (mode) { const normalizedMode = normalizeExecMode(mode); setGlobalExecMode(normalizedMode); setGlobalCompanyProfile(normalizedMode === 'org' ? 'custom' : normalizedMode === 'company' ? normalizeCompanyProfile(profile) : 'corporate') } else if (profile) setGlobalCompanyProfile(normalizeCompanyProfile(profile))
          } else if (type === 'project_run_updated' || type === 'seat_digest_updated' || type === 'work_item_batch_updated') {
            clientRef.current?.collabSync(getActiveProjectId(), undefined, projectViewGenerationRef.current)
          } else if (type === 'collab_sync_push' || type === 'project_index_push') {
            const syncScope = String((payload as Record<string, unknown>).sync_scope ?? (payload as Record<string, unknown>).syncScope ?? '').toLowerCase()
            const isProjectIndexPush = type === 'project_index_push' || syncScope === 'index'
            if (!payloadMatchesCurrentSwitch(payload)) return
            const syncData = mapCollabSyncPayload(payload)
            const syncProjectId = projectIdFromPayload(payload)
            if (!syncProjectId) return
            const applyingProjectSwitch = pendingProjectSwitchRef.current === syncProjectId || getActiveProjectId() !== syncProjectId
            activeProjectIdRef.current = syncProjectId; pendingProjectSwitchRef.current = null
            projectStoreRef.current?.setActiveProject(syncProjectId)
            if (applyingProjectSwitch) { clearPendingSessionDetailRefreshes(); setExecutionPanelTaskId(null); setCommsState(null); setCommsMessage(null) }
            setStatusDetail('')
            if (isProjectIndexPush) {
              const ss2 = sessionStoreRef.current
              if (ss2) ss2.initFromBackend(syncProjectId, syncData.sessions, { preserveExistingWhenIncomingPartial: true, preserveActiveWhenMissing: true })
              clientRef.current?.collabSync(syncProjectId, undefined, projectViewGenerationRef.current)
              return
            }
            const cs2 = chatStoreRef.current
            if (cs2) cs2.initFromBackend(syncProjectId, syncData.channels, syncData.messages)
            const bs2 = boardStoreRef.current
            if (bs2) bs2.initFromBackend(syncProjectId, syncData.boards, syncData.columns, syncData.tasks)
            const ss2 = sessionStoreRef.current
            if (ss2) { ss2.initFromBackend(syncProjectId, syncData.sessions); const activeId = ss2.activeSessionId; if (activeId) scheduleSessionDetailRefresh(activeId, 'full', true) }
          }
        } catch (e) { console.error('[onCollabMessage] Error:', e) }
      },
      onAgentRuntimeUpdate: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        if (payload.agent_id) { setSwarmAgents((prev) => { const next = prev.map((agent) => (agent.agent_id === payload.agent_id ? { ...agent, status: payload.status, runtime_status: payload.status, current_tool: payload.current_tool ?? undefined, current_task_id: payload.task_id ?? undefined } : agent)); swarmAgentsRef.current = next; return next }) }
        const bs = boardStoreRef.current
        if (!bs) return
        if (payload.task_id) {
          const rawPayload = payload as unknown as Record<string, unknown>
          const boardRuntimePatch = kanbanRuntimePatchFromPayload(rawPayload)
          const sessionRuntimePatch = sessionRuntimePatchFromPayload(rawPayload)
          if (hasOwnPayloadField(rawPayload, 'current_tool')) { const currentTool = typeof rawPayload.current_tool === 'string' && rawPayload.current_tool.trim() ? rawPayload.current_tool : undefined; boardRuntimePatch.currentTool = currentTool; sessionRuntimePatch.currentTool = currentTool; if (currentTool) { boardRuntimePatch.displayTool = currentTool; sessionRuntimePatch.displayTool = currentTool } }
          if (hasOwnPayloadField(rawPayload, 'display_tool')) { const displayTool = typeof rawPayload.display_tool === 'string' && rawPayload.display_tool.trim() ? rawPayload.display_tool : undefined; if (displayTool) { boardRuntimePatch.displayTool = displayTool; sessionRuntimePatch.displayTool = displayTool } }
          if (runtimeStatusClearsDisplayTool(payload.status)) { boardRuntimePatch.displayTool = undefined; sessionRuntimePatch.displayTool = undefined }
          if (hasOwnPayloadField(rawPayload, 'tool_elapsed_ms')) { const v = typeof rawPayload.tool_elapsed_ms === 'number' ? rawPayload.tool_elapsed_ms : undefined; boardRuntimePatch.toolElapsedMs = v; sessionRuntimePatch.toolElapsedMs = v }
          if (hasOwnPayloadField(rawPayload, 'last_tool_summary')) { const v = typeof rawPayload.last_tool_summary === 'string' ? rawPayload.last_tool_summary : undefined; boardRuntimePatch.lastToolSummary = v; sessionRuntimePatch.lastToolSummary = v }
          if (typeof payload.iteration === 'number') boardRuntimePatch.iterationCount = payload.iteration
          bs.updateTask(payload.task_id, { agentStatus: payload.status, ...boardRuntimePatch })
          const ss = sessionStoreRef.current
          if (ss) ss.updateSession(payload.task_id, { agentStatus: payload.status, ...sessionRuntimePatch, updatedAt: Date.now() })
          scheduleSessionDetailRefresh(payload.task_id)
        }
      },
      onWorkerNotification: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const data = payload as Record<string, unknown>
        const taskId = typeof payload.task_id === 'string' ? payload.task_id : ''
        if (!taskId) return
        const notification = data as import('../types/kanban').WorkerNotification
        sessionStoreRef.current?.updateSession(taskId, { ...sessionRuntimePatchFromPayload(data), latestNotification: notification, updatedAt: Date.now() })
        boardStoreRef.current?.updateTask(taskId, { ...kanbanRuntimePatchFromPayload(data), latestNotification: notification })
        scheduleSessionDetailRefresh(taskId)
      },
      onKanbanViewData: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const bs = boardStoreRef.current
        if (!bs) return
        bs.initFromBackend(projectIdFromPayload(payload as unknown as Record<string, unknown>) || getActiveProjectId(), (payload.boards ?? []).map(mapBackendBoard), (payload.columns ?? []).map(mapBackendColumn), (payload.tasks ?? []).map(mapBackendTask))
      },
      onSessionProgress: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss || !payload.task_id || !payload.entry) return
        ss.appendProgress(payload.task_id, { type: payload.entry.type as any, summary: payload.entry.summary, detail: payload.entry.detail, timestamp: payload.entry.timestamp * 1000, turnId: typeof payload.entry.turn_id === 'string' ? payload.entry.turn_id : typeof payload.entry.turnId === 'string' ? payload.entry.turnId : undefined, itemId: typeof payload.entry.item_id === 'string' ? payload.entry.item_id : typeof payload.entry.itemId === 'string' ? payload.entry.itemId : undefined, streamId: typeof payload.entry.stream_id === 'string' ? payload.entry.stream_id : typeof payload.entry.streamId === 'string' ? payload.entry.streamId : undefined, toolCallId: typeof payload.entry.tool_call_id === 'string' ? payload.entry.tool_call_id : typeof payload.entry.toolCallId === 'string' ? payload.entry.toolCallId : undefined, permissionGroupKey: typeof payload.entry.permission_group_key === 'string' ? payload.entry.permission_group_key : typeof payload.entry.permissionGroupKey === 'string' ? payload.entry.permissionGroupKey : undefined, seq: typeof payload.entry.seq === 'number' ? payload.entry.seq : undefined, executionMode: typeof payload.entry.execution_mode === 'string' ? payload.entry.execution_mode : typeof payload.entry.executionMode === 'string' ? payload.entry.executionMode : undefined })
        if (payload.entry.type === 'tool_call') { const toolLabel = String(payload.entry.summary ?? '').trim(); ss.updateSession(payload.task_id, { ...(toolLabel ? { currentTool: toolLabel, displayTool: toolLabel } : {}), updatedAt: payload.entry.timestamp * 1000 }) }
        if (payload.entry.type !== 'thinking' && payload.entry.type !== 'verification') scheduleSessionDetailRefresh(payload.task_id)
      },
      onBoardEvent: (payload) => {
        if (!payloadMatchesActiveProject(payload, false)) return
        const bs = boardStoreRef.current
        if (!bs) return
        const taskId = String(payload.task_id ?? '')
        if (!taskId) return
        const ss = sessionStoreRef.current
        if (ss) { const session = ss.sessions.find(s => s.taskId === taskId); if (session?.mode === 'child') return }
        const assigneeIds = Array.isArray(payload.assignee_ids) ? payload.assignee_ids.map(String) : []
        const workItemIdentity = workItemIdentityPatchFromPayload(payload)
        const existing = bs.tasks.find(t => t.id === taskId)
        if (existing) { const partial: Partial<import('../types/kanban').KanbanTask> = {}; if (payload.title) partial.title = String(payload.title); if (payload.display_id) partial.displayId = String(payload.display_id); if (assigneeIds.length > 0) partial.assigneeIds = assigneeIds; Object.assign(partial, workItemIdentity); if (Object.keys(partial).length > 0) bs.updateTask(taskId, partial); return }
        const boardId = String(payload.board_id ?? bs.activeBoardId ?? 'default')
        const boardCols = bs.columns.filter(c => c.boardId === boardId)
        const todoCol = boardCols.find(c => c.name === 'Todo')
        const columnId = todoCol?.id ?? boardCols[0]?.id ?? ''
        if (!columnId) return
        bs.createTask({ boardId, columnId, title: String(payload.title ?? 'Untitled'), taskId, displayId: payload.display_id ? String(payload.display_id) : undefined, assigneeIds })
        if (Object.keys(workItemIdentity).length > 0) bs.updateTask(taskId, workItemIdentity)
      },
      onSessionCreated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss) return
          const taskId = String(payload.task_id ?? '')
          if (!taskId) return
          const eventProjectId = projectIdFromPayload(payload as unknown as Record<string, unknown>)
          if (!eventProjectId) return
          const existing = taskId ? ss.sessions.find(s => s.taskId === taskId) : undefined
          const workItemIdentity = workItemIdentityPatchFromPayload(payload)
          const normalizedSessionExecMode = normalizeExecMode(payload.exec_mode ?? existing?.execMode)
          const payloadCompanyProfile = companyProfileForExecMode(normalizedSessionExecMode, payload.company_profile ?? existing?.companyProfile)
          const payloadOrgId = orgIdForExecMode(normalizedSessionExecMode, payload.org_id ?? payload.organization_id ?? existing?.orgId)
          if (normalizedSessionExecMode === 'org' && payloadOrgId) setActiveSavedOrg(payloadOrgId)
          ss.createSession({ projectId: eventProjectId, taskId, channelId: payload.channel_id, sessionId: payload.session_id, parentSessionId: payload.parent_session_id, originTaskId: payload.origin_task_id ?? existing?.originTaskId ?? taskId, mode: payload.parent_session_id ? 'child' : 'primary', execMode: normalizedSessionExecMode, companyProfile: payloadCompanyProfile, orgId: payloadOrgId, preferredAgent: normalizeTaskPreferredAgent(payload.preferred_agent ?? existing?.preferredAgent), title: payload.title, status: payload.status, columnId: existing?.columnId ?? 'todo', assigneeIds: Array.isArray(payload.assignee_ids) ? payload.assignee_ids.map(String) : (existing?.assigneeIds ?? []), priority: existing?.priority ?? null, tags: existing?.tags ?? [], progressLog: existing?.progressLog ?? [], createdAt: typeof payload.created_at === 'number' ? payload.created_at * 1000 : (existing?.createdAt ?? Date.now()), updatedAt: Date.now(), messageCount: 0, ...workItemIdentity })
          const bs = boardStoreRef.current
          const execMode = normalizedSessionExecMode
          if (bs && execMode === 'task' && !payload.parent_session_id && taskId && !bs.tasks.find(t => t.id === taskId) && bs.activeBoardId) { const boardCols = bs.columns.filter(c => c.boardId === bs.activeBoardId); const todoCol = boardCols.find(c => c.name === 'Todo'); if (todoCol) bs.createTask({ boardId: bs.activeBoardId, columnId: todoCol.id, title: payload.title, taskId, assigneeIds: existing?.assigneeIds ?? [] }) }
          if (pendingSessionCreateRef.current) { if (pendingSessionCreateProjectIdRef.current !== eventProjectId) return; clearPendingSessionCreate(); ss.setActiveSession(payload.task_id); scheduleSessionDetailRefresh(payload.task_id, 'full', true) }
        } catch (e) { console.error('[onSessionCreated] Error:', e) }
      },
      onSessionUpdated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss || !payload.task_id) return
          const nextExecMode = normalizeExecMode(payload.exec_mode ?? ss.sessions.find(s => s.taskId === payload.task_id)?.execMode)
          ss.updateSession(payload.task_id, { ...(payload.exec_mode ? { execMode: nextExecMode } : {}), ...(payload.exec_mode || payload.company_profile ? { companyProfile: companyProfileForExecMode(nextExecMode, payload.company_profile) } : {}), ...('org_id' in payload || 'organization_id' in payload ? { orgId: orgIdForExecMode(nextExecMode, payload.org_id ?? payload.organization_id) } : {}), ...(payload.preferred_agent ? { preferredAgent: normalizeTaskPreferredAgent(payload.preferred_agent) } : {}), ...(payload.selected_execution_agent ? { selectedExecutionAgent: normalizeTaskPreferredAgent(payload.selected_execution_agent) } : {}) })
          if ((payload.exec_mode === 'org' || payload.exec_mode === 'custom') && (payload.org_id || payload.organization_id)) setActiveSavedOrg(String(payload.org_id ?? payload.organization_id))
        } catch (e) { console.error('[onSessionUpdated] Error:', e) }
      },
      onSessionMessage: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload, false)) return
          const cs = chatStoreRef.current
          if (!cs) return
          const mapped = mapBackendMessage(payload)
          cs.addMessageFromBackend(mapped)
          const taskId = mapped.channelId.startsWith('session:') ? mapped.channelId.slice('session:'.length) : ''
          const terminalTurnId = terminalAssistantTurnId(mapped)
          const activeDraftTurnId = String(sessionStoreRef.current?.sessions.find(session => session.taskId === taskId)?.draftTurnId ?? '').trim()
          if (taskId && mapped.sender !== 'user') { if (terminalTurnId && terminalTurnId === activeDraftTurnId) sessionStoreRef.current?.clearDraft(taskId); scheduleSessionDetailRefresh(taskId, 'full', true) }
        } catch (e) { console.error('[onSessionMessage] Error:', e) }
      },
      onSessionTitleUpdated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (ss) ss.updateSession(payload.task_id, { title: payload.title })
          const bs = boardStoreRef.current
          if (bs) { const session = ss?.sessions.find(s => s.taskId === payload.task_id); const boardId = session?.originTaskId ?? payload.task_id; const board = bs.boards.find(b => b.id === boardId); if (board && board.name !== payload.title) bs.updateBoardName(boardId, payload.title) }
        } catch (e) { console.error('[onSessionTitleUpdated] Error:', e) }
      },
      onSessionDeleted: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss) return
          if (ss.activeSessionId === payload.task_id) ss.setActiveSession(null)
          ss.deleteSession(payload.task_id)
          const bs = boardStoreRef.current
          if (bs) bs.deleteTask(payload.task_id)
          const cs = chatStoreRef.current
          if (cs) cs.removeSessionData(payload.task_id)
        } catch (e) { console.error('[onSessionDeleted] Error:', e) }
      },
      onProjectSwitched: (payload) => {
        if (!payloadMatchesCurrentSwitch(payload as unknown as Record<string, unknown>)) return
        const projectId = typeof payload.project_id === 'string' ? payload.project_id.trim() : ''
        if (!projectId) return
        pendingProjectSwitchRef.current = projectId
        setStatusDetail(`Switching to ${projectId}...`)
      },
      onProjectDeleted: (payload) => { const ps = projectStoreRef.current; if (ps) ps.removeProject(payload.project_id) },
      onOrgInfo: (payload) => {
        const normalized = normalizeOrgInfoPayload(payload)
        setOrgInfoData(normalized)
        setSavedOrgVersionAtLoad(prev => prev === null ? (normalized?.org_version ?? 0) : prev)
        if (payload?.project_run?.execution_model === 'multi_team_org' || (Array.isArray(payload?.work_items) && payload.work_items.length > 0)) clientRef.current?.collabSync(getActiveProjectId(), undefined, projectViewGenerationRef.current)
      },
      onCommsState: (payload) => { if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return; setCommsState(payload) },
      onCommsMessage: (payload) => { if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, true)) return; setCommsMessage(payload) },
      onTalentList: (payload) => { setTalentTemplates(payload.templates ?? []) },
      onEmployeeDetail: (payload) => { setEmployeeDetail(payload) },
      onReorgList: (payload) => { setReorgProposals(payload.proposals ?? []) },
      onOrgConfigExport: (payload) => { setConfigExportYaml(payload.yaml ?? '') },
      // Fires only on manual YAML import via ConfigImportExportPanel.
      onOrgConfigImport: (payload) => {
        if (payload.ok) { setConfigImportPreview(payload.preview ?? null); setConfigImportError(null); if (!payload.dry_run) { setActiveSavedOrg(null); setSavedOrgVersionAtLoad(null) } } else { setConfigImportError(payload.error ?? 'Import failed'); setConfigImportPreview(null) }
      },
      onOrgSavedList: (payload) => { setSavedOrgsList(payload.orgs ?? []); if ('active_name' in payload) setActiveSavedOrg(payload.active_name ?? null) },
      onOrgSavedSaveAs: (payload) => {
        if (payload.ok) { clientRef.current?.orgSavedList(); setActiveSavedOrg(payload.name); setSavedOrgVersionAtLoad(null); setOrgToast({ kind: 'ok', text: `Saved "${payload.name}"` }) } else { setOrgToast({ kind: 'error', text: `Save failed: ${payload.error ?? 'unknown'}` }) }
      },
      onOrgSavedCreate: (payload) => {
        setOrgCreatePending(false); setOrgCreateResult({ ...payload, nonce: Date.now() })
        if (payload.ok) { const orgId = payload.organization_id || payload.name; setActiveSavedOrg(orgId); setGlobalExecMode('org'); setGlobalCompanyProfile('custom'); clientRef.current?.orgSavedList(); clientRef.current?.orgInfo(); setSavedOrgVersionAtLoad(null); setOrgToast({ kind: 'ok', text: `Created "${payload.organization_name || orgId}"` }) } else { setOrgToast({ kind: 'error', text: `Create failed: ${payload.error ?? 'unknown'}` }) }
      },
      onOrgSavedLoad: (payload) => {
        if (payload.ok) { setActiveSavedOrg(payload.name); setGlobalExecMode('org'); setGlobalCompanyProfile('custom'); clientRef.current?.orgSavedList(); setSavedOrgVersionAtLoad(null); setOrgToast({ kind: 'ok', text: `Loaded "${payload.name}"` }) } else { setOrgToast({ kind: 'error', text: `Load failed: ${payload.error ?? 'unknown'}` }) }
      },
      onOrgSavedDelete: (payload) => {
        if (payload.ok) { clientRef.current?.orgSavedList(); setActiveSavedOrg(prev => { if (prev === payload.name) { setSavedOrgVersionAtLoad(null); return null }; return prev }); setOrgToast({ kind: 'ok', text: `Deleted "${payload.name}"` }) } else { setOrgToast({ kind: 'error', text: `Delete failed: ${payload.error ?? 'unknown'}` }) }
      },
      onMarketBrowse: (payload) => { setMarketPresets((payload as any).presets ?? []) },
      onMarketPreview: (payload) => { setMarketPreviewData(payload as any) },
      onChildSessionCreated: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss) return
        const workItemIdentity = workItemIdentityPatchFromPayload(payload as Record<string, unknown>)
        const childProjectId = projectIdFromPayload(payload as unknown as Record<string, unknown>)
        if (!childProjectId) return
        ss.createSession({ projectId: childProjectId, taskId: payload.task_id, channelId: (payload as any).channel_id ?? `session:${payload.task_id}`, sessionId: payload.session_id, parentSessionId: payload.parent_session_id, originTaskId: payload.origin_task_id ?? payload.task_id, mode: 'child', orgId: String((payload as any).org_id ?? (payload as any).organization_id ?? '').trim() || undefined, title: payload.title, status: 'pending', columnId: 'todo', assigneeIds: payload.agent_id ? [payload.agent_id] : [], priority: null, tags: [], createdAt: Date.now(), updatedAt: Date.now(), messageCount: 0, progressLog: [], ...workItemIdentity })
        if (payload.parent_session_id) { const parent = ss.sessions.find(s => s.sessionId === payload.parent_session_id || s.taskId === payload.parent_session_id); if (parent && !parent.isCompanyRuntime) ss.setCompanyRuntime(parent.taskId, true) }
      },
      onWorkItemProgress: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss || !payload.task_id || !payload.entry) return
        const entryProjectionId = typeof payload.entry.work_item_projection_id === 'string' ? payload.entry.work_item_projection_id : ''
        const projectionId = entryProjectionId && entryProjectionId !== 'company_runtime' ? entryProjectionId : undefined
        const runtimeTaskId = typeof payload.entry.runtime_task_id === 'string' && payload.entry.runtime_task_id ? payload.entry.runtime_task_id : typeof payload.runtime_task_id === 'string' && payload.runtime_task_id ? payload.runtime_task_id : typeof payload.entry.execution_turn_id === 'string' && payload.entry.execution_turn_id ? payload.entry.execution_turn_id : typeof payload.execution_turn_id === 'string' && payload.execution_turn_id ? payload.execution_turn_id : undefined
        const executionTurnId = typeof payload.entry.execution_turn_id === 'string' && payload.entry.execution_turn_id ? payload.entry.execution_turn_id : typeof payload.execution_turn_id === 'string' && payload.execution_turn_id ? payload.execution_turn_id : runtimeTaskId
        ss.updateSession(payload.task_id, { isCompanyRuntime: true, ...(projectionId ? { workItemProjectionId: projectionId } : {}) })
        const bs = boardStoreRef.current
        if (bs && projectionId) bs.updateTask(payload.task_id, { workItemProjectionId: projectionId })
        ss.appendWorkItemProgress(payload.task_id, { timestamp: payload.entry.timestamp * 1000, type: payload.entry.type as any, workItemProjectionId: projectionId, workItemTurnType: typeof payload.entry.work_item_turn_type === 'string' ? payload.entry.work_item_turn_type : undefined, workItemProjectionTitle: typeof payload.entry.work_item_projection_title === 'string' ? payload.entry.work_item_projection_title : undefined, runtimeTaskId, executionTurnId, roleName: payload.entry.role_name, detail: payload.entry.detail })
      },
    })
    clientRef.current = client
    client.connect()
    return () => {
      client.disconnect()
      for (const tid of timersRef.current) clearTimeout(tid)
      timersRef.current.clear()
      for (const tid of pendingSessionDetailRefreshRef.current.values()) clearTimeout(tid)
      pendingSessionDetailRefreshRef.current.clear()
      if (deltaFlushTimerRef.current !== null) { window.clearTimeout(deltaFlushTimerRef.current); deltaFlushTimerRef.current = null }
      pendingDeltaFlushRef.current.clear()
      if (uiTickTimerRef.current !== null) { window.clearTimeout(uiTickTimerRef.current); uiTickTimerRef.current = null }
    }
  }, [wsUrl])

  // Visibility refresh
  useEffect(() => {
    const refreshProjectState = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      const now = Date.now()
      if (now - lastProjectIndexRefreshRef.current < 1_500) return
      lastProjectIndexRefreshRef.current = now
      clientRef.current?.collabSync(getActiveProjectId(), undefined, projectViewGenerationRef.current)
    }
    const handleVisibilityChange = () => { if (document.visibilityState === 'visible') refreshProjectState() }
    window.addEventListener('focus', refreshProjectState)
    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => { window.removeEventListener('focus', refreshProjectState); document.removeEventListener('visibilitychange', handleVisibilityChange) }
  }, [getActiveProjectId])

  // Auto-clear org-toast
  useEffect(() => { if (!orgToast) return; const t = setTimeout(() => setOrgToast(null), 3000); return () => clearTimeout(t) }, [orgToast])

  // Last task done celebration
  useEffect(() => {
    if (!lastTaskDoneAgent) return
    const agentId = lastTaskDoneAgent
    bridgeRef.current.setAgentBubble(agentId, 'Done!')
    const tid = setTimeout(() => { timersRef.current.delete(tid); bridgeRef.current.setAgentBubble(agentId, null) }, 3000)
    timersRef.current.add(tid)
    setLastTaskDoneAgent(null)
  }, [lastTaskDoneAgent])

  // Stable org callbacks
  const handleSavedOrgsList = useCallback(() => { clientRef.current?.orgSavedList() }, [])
  const handleSavedOrgSaveAs = useCallback((name: string, overwrite: boolean) => { clientRef.current?.orgSavedSaveAs(name, overwrite) }, [])
  const handleSavedOrgCreate = useCallback((organizationName: string, members: OrgCreateMemberInput[]) => { setOrgCreatePending(true); setOrgCreateResult(null); clientRef.current?.orgSavedCreate(organizationName, members) }, [])
  const handleSavedOrgLoad = useCallback((name: string) => { clientRef.current?.orgSavedLoad(name) }, [])
  const handleSavedOrgDelete = useCallback((name: string) => { clientRef.current?.orgSavedDelete(name) }, [])
  const handleSelectCorporateOrg = useCallback(() => { setGlobalExecMode('company'); setGlobalCompanyProfile('corporate'); setSavedOrgVersionAtLoad(null); clientRef.current?.setExecutionMode('company', 'corporate', globalTaskPreferredAgent); clientRef.current?.orgInfo() }, [globalTaskPreferredAgent])

  const applyWsUrl = useCallback(() => { const next = wsUrlInput.trim(); if (!next || next === wsUrl) return; setWsUrl(next) }, [wsUrlInput, wsUrl])

  return {
    clientRef, wsUrl, wsUrlInput, setWsUrlInput, applyWsUrl, status, statusDetail, snapshot, events, uiTick, setUiTick,
    swarmAgents, swarmAgentsRef, globalExecMode, setGlobalExecMode, globalCompanyProfile, setGlobalCompanyProfile,
    globalTaskPreferredAgent, setGlobalTaskPreferredAgent, orgInfoData, commsState, commsMessage,
    talentTemplates, employeeDetail, reorgProposals, marketPresets, marketPreviewData, setMarketPreviewData,
    configExportYaml, configImportPreview, configImportError, savedOrgsList, activeSavedOrg, setActiveSavedOrg,
    savedOrgVersionAtLoad, orgCreatePending, orgCreateResult, orgToast, hiringTemplateId, setHiringTemplateId,
    toastMessage, toastType, confirmDeleteId, setConfirmDeleteId, deletingAgentId, setDeletingAgentId,
    executionPanelTaskId, setExecutionPanelTaskId,
    boardStore, chatStore, sessionStore, projectStore,
    getActiveProjectId, beginProjectSwitch, beginPendingSessionCreate, scheduleSessionDetailRefresh,
    projectViewGenerationRef, pendingProjectSwitchRef, pendingSessionCreateRef,
    sessionRecruitmentByRole, normalizeProjectId,
    handleSavedOrgsList, handleSavedOrgSaveAs, handleSavedOrgCreate, handleSavedOrgLoad, handleSavedOrgDelete,
    handleSelectCorporateOrg, runtimeLogsAckHandlersRef, timersRef, bridgeRef,
  }
}

