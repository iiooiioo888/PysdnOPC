import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useI18n } from './lib/i18n'
import { PhaserGame } from './game/PhaserGame'
import { GameBridge } from './game/GameBridge'
import { CollisionEditor } from './components/CollisionEditor'
import { registerTestRunner } from './game/test/eventTestRunner'
import { getOffices } from './game/map/OfficeStore'
import { getOfficeDeskSeats } from './game/map/InteractionZones'
import type { OrgCreateMemberInput, TaskPreferredAgent } from './types/visual'
import { WorkspacePage } from './workspace/WorkspacePage'
import { DashboardPage } from './dashboard/DashboardPage'
import { RoleProfilePage } from './dashboard/RoleProfilePage'
import { TemplatesPage } from './dashboard/TemplatesPage'
import { LlmSettingsPage } from './settings/LlmSettingsPage'
import { AgentSettingsPage } from './settings/AgentSettingsPage'
import './dashboard/dashboard.css'
import './dashboard/templates.css'
import { ExecutionPanel } from './kanban/ExecutionPanel'
import { ProjectSelector } from './components/ProjectSelector'
import { OrgTab } from './org/OrgTab'
import { MaybeExecutionPanel } from './components/MaybeExecutionPanel'
import { notifyTaskAssigned } from './lib/taskChatBridge'
import { useAppWebSocket } from './hooks/useAppWebSocket'
import { readOutdoorOverrideUi, statusClass, truncateJson, normalizeExecMode, normalizeCompanyProfile, companyProfileForExecMode, orgIdForExecMode, normalizeTaskPreferredAgent } from './lib/appUtils'
import type { ThemeName, AppPage } from './types/app'

export default function App() {
  const { t } = useI18n()
  const bridgeRef = useRef(new GameBridge())
  useMemo(() => registerTestRunner(bridgeRef.current), [])

  const ws = useAppWebSocket(bridgeRef)

  // ── UI-only state ──
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  const [theme, setTheme] = useState<ThemeName>('openopc')
  const [showSubagents, setShowSubagents] = useState(true)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return localStorage.getItem('opc_office_sidebar_collapsed') === '1' } catch { return false }
  })
  const toggleSidebar = () => setSidebarCollapsed(v => {
    const next = !v
    try { localStorage.setItem('opc_office_sidebar_collapsed', next ? '1' : '0') } catch { /* private mode */ }
    return next
  })
  const [eventTypeFilter, setEventTypeFilter] = useState('all')
  const [activePage, setActivePage] = useState<AppPage>('workspace')
  const [showDevTools, setShowDevTools] = useState(false)
  const [outdoorOverride, setOutdoorOverride] = useState<'auto' | 'day' | 'night'>(() => readOutdoorOverrideUi())
  const [editingOfficeName, setEditingOfficeName] = useState<string | null>(null)
  const [officeNameDraft, setOfficeNameDraft] = useState('')
  const [settingsTab, setSettingsTab] = useState<'llm' | 'agents'>('llm')

  // Listen for agent selection from Phaser
  useEffect(() => {
    const bridge = bridgeRef.current
    const handler = (agentId: string) => { setSelectedAgentId(agentId); ws.setUiTick(n => n + 1) }
    bridge.on('agentSelected', handler)
    return () => { bridge.off('agentSelected', handler) }
  }, [])

  // ── Session action callbacks ──
  const handleSessionModeChange = useCallback((taskId: string, mode: string, profile?: string, orgId?: string) => {
    const existingSession = ws.sessionStore.sessions.find(session => session.taskId === taskId)
    const normalizedMode = normalizeExecMode(mode)
    const currentProfile = normalizeCompanyProfile(existingSession?.companyProfile ?? ws.globalCompanyProfile)
    const nextProfile = normalizedMode === 'org' ? 'custom' : normalizedMode === 'company' ? 'corporate' : 'corporate'
    const currentOrgId = String(existingSession?.orgId ?? ws.activeSavedOrg ?? '').trim()
    const nextOrgId = orgIdForExecMode(normalizedMode, orgId ?? existingSession?.orgId ?? ws.activeSavedOrg)
    const currentSessionMode = normalizeExecMode(existingSession?.execMode)
    if (currentSessionMode === normalizedMode && currentProfile === nextProfile && currentOrgId === String(nextOrgId ?? '').trim() && ws.globalExecMode === normalizedMode && normalizeCompanyProfile(ws.globalCompanyProfile) === nextProfile) return
    ws.sessionStore.updateSession(taskId, { execMode: normalizedMode, companyProfile: nextProfile, orgId: nextOrgId, preferredAgent: existingSession?.preferredAgent ?? ws.globalTaskPreferredAgent })
    ws.setGlobalExecMode(normalizedMode)
    ws.setGlobalCompanyProfile(nextProfile)
    if (normalizedMode === 'org' && nextOrgId) ws.setActiveSavedOrg(nextOrgId)
    const nextPreferredAgent = existingSession?.preferredAgent ?? ws.globalTaskPreferredAgent
    const runtimeProfile = normalizedMode === 'task' ? undefined : nextProfile
    ws.clientRef.current?.sessionUpdateConfig(ws.getActiveProjectId(), taskId, normalizedMode, runtimeProfile, nextPreferredAgent, nextOrgId)
    ws.clientRef.current?.setExecutionMode(normalizedMode, runtimeProfile, nextPreferredAgent, nextOrgId)
  }, [ws.sessionStore, ws.globalExecMode, ws.globalCompanyProfile, ws.globalTaskPreferredAgent, ws.activeSavedOrg, ws.getActiveProjectId])

  const handleSessionTaskAgentChange = useCallback((taskId: string, preferredAgent: TaskPreferredAgent) => {
    const existingSession = ws.sessionStore.sessions.find(session => session.taskId === taskId)
    const normalizedPreferredAgent = normalizeTaskPreferredAgent(preferredAgent)
    const normalizedMode = normalizeExecMode(existingSession?.execMode)
    const nextProfile = normalizedMode === 'org' ? 'custom' : normalizedMode === 'company' ? 'corporate' : 'corporate'
    ws.sessionStore.updateSession(taskId, { preferredAgent: normalizedPreferredAgent, selectedExecutionAgent: normalizedPreferredAgent })
    ws.setGlobalTaskPreferredAgent(normalizedPreferredAgent)
    const runtimeProfile = normalizedMode === 'task' ? undefined : nextProfile
    const orgId = orgIdForExecMode(normalizedMode, existingSession?.orgId ?? ws.activeSavedOrg)
    ws.clientRef.current?.sessionUpdateConfig(ws.getActiveProjectId(), taskId, normalizedMode, runtimeProfile, normalizedPreferredAgent, orgId)
    ws.clientRef.current?.setExecutionMode(normalizedMode, runtimeProfile, normalizedPreferredAgent, orgId)
  }, [ws.sessionStore, ws.globalCompanyProfile, ws.activeSavedOrg, ws.getActiveProjectId])

  const handleContinueInNewChat = useCallback((mode: 'task' | 'company' | 'org' | 'custom', profile?: 'corporate' | 'custom', orgId?: string) => {
    if (ws.pendingProjectSwitchRef.current || ws.pendingSessionCreateRef.current) return
    const projectId = ws.getActiveProjectId()
    ws.beginPendingSessionCreate(projectId)
    const normalizedMode = normalizeExecMode(mode)
    const resolvedProfile: 'corporate' | 'custom' | undefined = normalizedMode === 'org' ? 'custom' : normalizedMode === 'company' ? 'corporate' : undefined
    ws.clientRef.current?.createSession(projectId, undefined, normalizedMode, resolvedProfile, ws.globalTaskPreferredAgent, orgIdForExecMode(normalizedMode, orgId ?? ws.activeSavedOrg))
    setActivePage('workspace')
  }, [ws.getActiveProjectId, ws.beginPendingSessionCreate, ws.globalTaskPreferredAgent, ws.activeSavedOrg])

  const markRuntimeControlForTask = useCallback((taskId: string, patch: Partial<import('./types/kanban').Session>) => {
    const session = ws.sessionStore.sessions.find(s => s.taskId === taskId)
    const parentSessionId = session?.resumeParentSessionId ?? session?.parentSessionId ?? session?.sessionId
    for (const candidate of ws.sessionStore.sessions) {
      if (candidate.taskId === taskId || (!!parentSessionId && (candidate.parentSessionId === parentSessionId || candidate.sessionId === parentSessionId))) {
        ws.sessionStore.updateSession(candidate.taskId, { ...patch, resumeParentSessionId: parentSessionId })
      }
    }
  }, [ws.sessionStore])

  const handleSessionStop = useCallback((taskId: string) => {
    const session = ws.sessionStore.sessions.find(s => s.taskId === taskId)
    const isCompanyRuntime = session?.execMode === 'company' || session?.execMode === 'org' || session?.execMode === 'custom' || !!session?.isCompanyRuntime || !!session?.parentSessionId || !!session?.companyProfile
    if (isCompanyRuntime) markRuntimeControlForTask(taskId, { runtimeControlState: 'suspending', canStop: false, canResume: false })
    ws.clientRef.current?.sessionStop(ws.getActiveProjectId(), taskId)
  }, [ws.sessionStore.sessions, markRuntimeControlForTask, ws.getActiveProjectId])

  const handleSessionResume = useCallback((taskId: string, runtimeSessionId?: string, checkpointId?: string) => {
    const session = ws.sessionStore.sessions.find(s => s.taskId === taskId)
    const isCompanyRuntime = session?.execMode === 'company' || session?.execMode === 'org' || session?.execMode === 'custom' || !!session?.isCompanyRuntime || !!session?.parentSessionId || !!session?.companyProfile
    if (isCompanyRuntime) markRuntimeControlForTask(taskId, { runtimeControlState: 'resuming', canStop: false, canResume: false })
    ws.clientRef.current?.sessionResume(ws.getActiveProjectId(), taskId, runtimeSessionId ?? session?.resumeParentSessionId ?? session?.parentSessionId ?? session?.sessionId, checkpointId ?? session?.pendingRuntimeCheckpointId)
  }, [ws.sessionStore.sessions, markRuntimeControlForTask, ws.getActiveProjectId])

  const handleGlobalModeChange = useCallback((mode: 'task' | 'company' | 'org' | 'custom', profile?: string, orgId?: string) => {
    const normalizedMode = normalizeExecMode(mode)
    const nextProfile = normalizedMode === 'org' ? 'custom' : normalizedMode === 'company' ? 'corporate' : 'corporate'
    const nextOrgId = orgIdForExecMode(normalizedMode, orgId ?? ws.activeSavedOrg)
    ws.setGlobalExecMode(normalizedMode)
    ws.setGlobalCompanyProfile(nextProfile)
    if (nextOrgId) ws.setActiveSavedOrg(nextOrgId)
    ws.clientRef.current?.setExecutionMode(normalizedMode, normalizedMode === 'task' ? undefined : nextProfile, ws.globalTaskPreferredAgent, nextOrgId)
  }, [ws.globalCompanyProfile, ws.globalTaskPreferredAgent, ws.activeSavedOrg])

  const handleTaskAssigned = useCallback((taskId: string, agentIds: string[], taskTitle: string) => {
    const task = ws.boardStore.tasks.find(t => t.id === taskId)
    for (const agentId of agentIds) {
      bridgeRef.current.sendToSeat(agentId)
      bridgeRef.current.setAgentActive(agentId, true)
      bridgeRef.current.setAgentBubble(agentId, `Task: ${taskTitle.slice(0, 22)}`)
      ws.clientRef.current?.assignTaskToAgent(ws.getActiveProjectId(), taskId, agentId, taskTitle)
    }
    if (task) { const names = agentIds.map(id => ws.swarmAgents.find(a => a.agent_id === id)?.name ?? id); notifyTaskAssigned(ws.chatStore, task, names) }
    ws.setUiTick(n => n + 1)
  }, [ws.boardStore.tasks, ws.chatStore, ws.swarmAgents, ws.getActiveProjectId])

  // ── Office page handlers ──
  const handleRenameOffice = (officeId: string) => {
    if (officeNameDraft.trim()) { bridgeRef.current.renameOffice(officeId, officeNameDraft.trim()); ws.setUiTick(t => t + 1) }
    setEditingOfficeName(null)
  }
  const handleAssignAgent = (officeId: string, agentId: string) => {
    bridgeRef.current.assignAgentToOffice(agentId, officeId)
    ws.clientRef.current?.moveAgent(agentId, officeId)
    ws.setUiTick(t => t + 1)
  }
  const handleChangeSeat = (agentId: string, seatId: string) => { bridgeRef.current.changeAgentSeat(agentId, seatId); ws.setUiTick(t => t + 1) }
  const selectAgent = useCallback((agentId: string) => { setSelectedAgentId(agentId); ws.setUiTick((n) => n + 1) }, [])

  // ── Memos ──
  const metrics = useMemo(() => {
    const totalAgents = ws.snapshot ? Object.keys(ws.snapshot.agents).length : 0
    const totalSkills = ws.snapshot?.skills.total ?? 0
    return { totalAgents, totalSkills }
  }, [ws.snapshot])

  const cards = useMemo(() => {
    const all = bridgeRef.current.getCharacterCards()
    const visible = showSubagents ? all : all.filter((c) => !c.isSubagent)
    return visible.slice().sort((a, b) => a.displayName.localeCompare(b.displayName))
  }, [showSubagents, ws.uiTick])

  const offices = useMemo(() => getOffices(), [ws.uiTick])
  const officeMap = useMemo(() => { const m: Record<string, string> = {}; for (const c of cards) { if (c.officeId) m[c.id] = c.officeId }; return m }, [cards])
  const selectedCard = cards.find((c) => c.id === selectedAgentId) ?? null
  const selectedAgentSeats = useMemo(() => { if (!selectedCard) return []; return bridgeRef.current.getSeatsForOffice(selectedCard.officeId) }, [selectedCard?.officeId, ws.uiTick])
  const evolutionPhases = useMemo(() => { const recent = ws.events.slice(-40); return { trace: recent.some((e) => e.type === 'tool_start' || e.type === 'tool_done'), reflect: recent.some((e) => e.type === 'reflect_start' || e.type === 'reflect_done'), synthesize: recent.some((e) => e.type === 'skill_synthesized') } }, [ws.events])
  const eventTypes = useMemo(() => { const uniq = Array.from(new Set(ws.events.map((evt) => evt.type))); return ['all', ...uniq] }, [ws.events])
  const filteredEvents = useMemo(() => { const list = eventTypeFilter === 'all' ? ws.events : ws.events.filter((evt) => evt.type === eventTypeFilter); return list.slice().reverse() }, [eventTypeFilter, ws.events])

  const isOrgMode = ws.globalExecMode === 'org'
  const globalModeLabel = ws.globalExecMode === 'task' ? 'task' : ws.globalExecMode === 'org' ? `company/${ws.activeSavedOrg ?? 'org'}` : `company/${ws.globalCompanyProfile}`

  return (
    <div className={`app-shell theme-${theme}`}>
      {ws.orgToast && (<div className={`org-toast org-toast--${ws.orgToast.kind}`} role="status" aria-live="polite">{ws.orgToast.text}</div>)}
      <header className="topbar">
        <div className="topbar-left">
          <span className="logo-text">Open<span className="logo-accent">OPC</span></span>
          <div className={`conn-dot ${statusClass(ws.status)}`} title={`${ws.status}${ws.statusDetail ? ` — ${ws.statusDetail}` : ''}\n${ws.wsUrl}`} />
          <ProjectSelector projects={ws.projectStore.projects} activeId={ws.projectStore.activeProjectId}
            onSelect={(id) => { const switchSeq = ws.beginProjectSwitch(id); ws.clientRef.current?.switchProject(id, switchSeq) }}
            onCreate={(id) => { ws.clientRef.current?.createProject(id) }}
            onDelete={(id) => ws.clientRef.current?.deleteProject(id)} />
        </div>
        <div className="topbar-center">
          <div className="page-nav">
            <button className={`page-nav-btn${activePage === 'workspace' ? ' active' : ''}`} onClick={() => setActivePage('workspace')}>
              {t('nav.workspace')}
              {(() => { const total = ws.chatStore.channels.reduce((sum, ch) => sum + ws.chatStore.getUnreadCount(ch.id), 0); return total > 0 ? <span className="nav-unread-badge">{total > 99 ? '99+' : total}</span> : null })()}
            </button>
            <button className={`page-nav-btn${activePage === 'dashboard' ? ' active' : ''}`} onClick={() => setActivePage('dashboard')}>📊 {t('nav.dashboard', '儀表盤')}</button>
            <button className={`page-nav-btn${activePage === 'roleProfile' ? ' active' : ''}`} onClick={() => setActivePage('roleProfile')}>🪪 {t('nav.roleProfile', '角色画像')}</button>
            <button className={`page-nav-btn${activePage === 'office' ? ' active' : ''}`} onClick={() => setActivePage('office')}>{t('nav.game')}</button>
            <button className={`page-nav-btn${activePage === 'org' ? ' active' : ''}`} onClick={() => setActivePage('org')}>{t('nav.org')}</button>
            <button className={`page-nav-btn${activePage === 'templates' ? ' active' : ''}`} onClick={() => setActivePage('templates')}>🏗️ {t('nav.templates', '模板')}</button>
            <button className={`page-nav-btn${activePage === 'settings' ? ' active' : ''}`} onClick={() => setActivePage('settings')}>⚙️ {t('nav.settings', '設定')}</button>
          </div>
          <div className="stat-chips">
            <span className="stat-chip"><b>{metrics.totalAgents}</b> {t('stats.agents')}</span>
            <span className="stat-chip"><b>{metrics.totalSkills}</b> {t('stats.skills')}</span>
            <span className="stat-chip"><b>{ws.boardStore.getOpenTaskCount()}</b> {t('stats.tasks')}</span>
          </div>
        </div>
        <div className="topbar-right">
          <select className="theme-select" value={outdoorOverride} title={t('app.outdoorTitle')} onChange={(e) => { const v = e.target.value as 'auto' | 'day' | 'night'; setOutdoorOverride(v); try { if (v === 'auto') { localStorage.removeItem('opc_outdoor_override'); localStorage.removeItem('opc_outdoor_day'); localStorage.removeItem('opc_outdoor_night') } else { localStorage.setItem('opc_outdoor_override', v); localStorage.removeItem('opc_outdoor_day'); localStorage.removeItem('opc_outdoor_night') } } catch { /* private mode */ }; bridgeRef.current.syncOutdoorLighting() }}>
            <option value="auto">Outdoor auto</option><option value="day">Outdoor day</option><option value="night">Outdoor night</option>
          </select>
          <select className="theme-select" value={theme} onChange={(e) => setTheme(e.target.value as ThemeName)}>
            <option value="midnight">Midnight</option><option value="neon">Neon</option><option value="paper">Paper</option><option value="retro">Retro</option><option value="terminal">Terminal</option><option value="cozy">Cozy</option><option value="openopc">OpenOPC</option>
          </select>
          <button className={`icon-btn ${showDevTools ? 'active' : ''}`} onClick={() => setShowDevTools((v) => !v)} title="Developer Tools">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M5.5 2L2 5.5 5.5 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/><path d="M10.5 7L14 10.5 10.5 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </button>
        </div>
      </header>

      {activePage === 'workspace' && (
        <WorkspacePage boardStore={ws.boardStore} chatStore={ws.chatStore} sessionStore={ws.sessionStore} agents={ws.swarmAgents} officeMap={officeMap} execMode={ws.globalExecMode} companyProfile={ws.globalCompanyProfile} taskPreferredAgent={ws.globalTaskPreferredAgent} projectId={ws.projectStore.activeProjectId} orgInfoData={ws.orgInfoData} onNavigateToOrg={() => setActivePage('org')} savedOrgsList={ws.savedOrgsList} activeSavedOrg={ws.activeSavedOrg} onSavedOrgsList={ws.handleSavedOrgsList} onSavedOrgLoad={ws.handleSavedOrgLoad} commsState={ws.commsState} commsMessage={ws.commsMessage}
          onCommsRefresh={(opts) => { const { project_id: _ignoredProjectId, ...scopedOpts } = opts ?? {}; ws.clientRef.current?.commsState(ws.getActiveProjectId(), scopedOpts) }}
          onCommsReadMessage={(path) => ws.clientRef.current?.commsReadMessage(ws.getActiveProjectId(), path)}
          onRunTask={(taskId, title, desc, mode, profile) => { ws.clientRef.current?.send({ type: 'run_task', project_id: ws.getActiveProjectId(), task_id: taskId, title, description: desc, mode, profile }) }}
          onCreateTask={(title, boardId, columnId, taskId) => { ws.clientRef.current?.send({ type: 'kanban_create_task', project_id: ws.getActiveProjectId(), title, board_id: boardId, column_id: columnId, task_id: taskId }) }}
          onMoveTask={(taskId, columnId) => { ws.clientRef.current?.send({ type: 'kanban_move_task', project_id: ws.getActiveProjectId(), task_id: taskId, column_id: columnId }) }}
          onCreateSession={() => { if (ws.pendingProjectSwitchRef.current || ws.pendingSessionCreateRef.current) return; const projectId = ws.getActiveProjectId(); ws.beginPendingSessionCreate(projectId); ws.clientRef.current?.createSession(projectId, undefined, ws.globalExecMode, companyProfileForExecMode(ws.globalExecMode, ws.globalCompanyProfile), ws.globalTaskPreferredAgent, orgIdForExecMode(ws.globalExecMode, ws.activeSavedOrg)) }}
          onSessionSend={(taskId, content, attachments, metadata) => ws.clientRef.current?.sessionSend(ws.getActiveProjectId(), taskId, content, attachments, metadata)}
          onSecretarySend={(content) => ws.clientRef.current?.secretarySend(ws.getActiveProjectId(), content)}
          onDeleteSession={(taskId) => ws.clientRef.current?.deleteSession(ws.getActiveProjectId(), taskId)}
          onTitleChange={(taskId, title) => ws.clientRef.current?.sessionUpdateTitle(ws.getActiveProjectId(), taskId, title)}
          onSessionConfigChange={handleSessionModeChange} onSessionTaskAgentChange={handleSessionTaskAgentChange} onContinueInNewChat={handleContinueInNewChat} onSessionStop={handleSessionStop} onSessionResume={handleSessionResume}
          onSessionComplete={(taskId) => ws.clientRef.current?.sessionComplete(ws.getActiveProjectId(), taskId)}
          onLoadSessionDetail={(taskId, opts) => { const client = ws.clientRef.current; if (!client) return; return client.sessionDetail(ws.getActiveProjectId(), taskId, { ...opts, include: opts?.detailLevel === 'full' ? ['messages', 'session_state', 'progress', 'work_items', 'runtime_context'] : ['messages', 'session_state'], viewGeneration: ws.projectViewGenerationRef.current }).then((payload) => { if (payload.ok === false) throw new Error(String(payload.error ?? 'session_detail failed')) }) }}
          onOpenExecutionPanel={(taskId) => ws.setExecutionPanelTaskId(taskId)}
          onCollabSync={() => ws.clientRef.current?.collabSync(ws.getActiveProjectId(), undefined, ws.projectViewGenerationRef.current)} />
      )}
      {activePage === 'dashboard' && (<DashboardPage sessions={ws.sessionStore.sessions} projectId={ws.getActiveProjectId()} sendRuntimeLogs={(pid, taskId) => ws.clientRef.current?.requestRuntimeLogs(pid, taskId)} onRuntimeLogsAck={(handler) => { ws.runtimeLogsAckHandlersRef.current.add(handler); return () => { ws.runtimeLogsAckHandlersRef.current.delete(handler) } }} />)}
      {activePage === 'roleProfile' && (<RoleProfilePage sessions={ws.sessionStore.sessions} projectId={ws.getActiveProjectId()} orgInfoData={ws.orgInfoData} sendRequest={(payload) => ws.clientRef.current?.send(payload)} onAck={(handler) => { ws.roleProfileAckHandlersRef.current.add(handler); return () => { ws.roleProfileAckHandlersRef.current.delete(handler) } }} />)}
      {activePage === 'templates' && (<TemplatesPage onApplyTemplate={(templateId) => { if (ws.clientRef.current) ws.clientRef.current.send(JSON.stringify({ action: 'apply_org_template', template_id: templateId })) }} />)}
      {activePage === 'settings' && (
        <div>
          <div className="settings-tabs">
            <button className={`settings-tab${settingsTab === 'llm' ? ' active' : ''}`} onClick={() => setSettingsTab('llm')}>🤖 {t('nav.llmSettings', 'LLM 模型')}</button>
            <button className={`settings-tab${settingsTab === 'agents' ? ' active' : ''}`} onClick={() => setSettingsTab('agents')}>🔧 {t('nav.agentSettings', '外部代理')}</button>
          </div>
          {settingsTab === 'llm' && <LlmSettingsPage wsClient={ws.clientRef.current} />}
          {settingsTab === 'agents' && <AgentSettingsPage wsClient={ws.clientRef.current} />}
        </div>
      )}
      {activePage === 'org' && (
        <div className="org-page">
          <OrgTab data={ws.orgInfoData} sessionRecruitmentByRole={ws.sessionRecruitmentByRole} talents={ws.talentTemplates} employeeDetail={ws.employeeDetail} reorgProposals={ws.reorgProposals} isCustomMode={isOrgMode}
            onRequestData={() => ws.clientRef.current?.orgInfo()} onRequestTalents={() => ws.clientRef.current?.talentList()} onRequestEmployeeDetail={(id) => ws.clientRef.current?.employeeDetail(id)}
            onHireTalent={(tid, rid) => { ws.setHiringTemplateId(tid); ws.clientRef.current?.talentHire(tid, rid, undefined, ws.orgInfoData?.organization_id || ws.activeSavedOrg || undefined) }} hiringTemplateId={ws.hiringTemplateId}
            onImportEmployee={(empId) => ws.clientRef.current?.importEmployeeAsAgent(empId)} onRequestReorgList={() => ws.clientRef.current?.reorgList()} onReorgDecide={(pid, approved, notes) => ws.clientRef.current?.reorgDecide(pid, approved, notes)}
            onMarketExport={(data) => ws.clientRef.current?.marketExport(data)} onMarketInstall={(path, strategy) => ws.clientRef.current?.marketInstall(path, strategy)} onMarketUninstall={(pkgId) => ws.clientRef.current?.marketUninstall(pkgId)}
            marketPresets={ws.marketPresets} marketPreviewData={ws.marketPreviewData} onMarketBrowse={() => ws.clientRef.current?.marketBrowse()} onMarketPreview={(id) => ws.clientRef.current?.marketPreview(id)} onMarketApplyPreset={(id, strategy) => ws.clientRef.current?.marketApplyPreset(id, strategy)} onMarketClearPreview={() => ws.setMarketPreviewData(null)}
            onAddRole={(rid, name, resp, rt, icon) => ws.clientRef.current?.addRole(rid, name, resp, rt, icon)} onBulkAddRoles={(roles) => ws.clientRef.current?.bulkAddRoles(roles)} onUpdateRole={(rid, updates) => ws.clientRef.current?.updateRole(rid, updates)} onDeleteRole={(rid) => ws.clientRef.current?.deleteRole(rid)}
            onUpdateOrgStrategy={(data) => ws.clientRef.current?.updateOrgStrategy(data)} onUpdateRuntimePolicy={(policy) => ws.clientRef.current?.updateRuntimePolicy(policy)} onResetArchitecture={() => ws.clientRef.current?.resetArchitecture()}
            onConfigExport={() => ws.clientRef.current?.orgConfigExport()} onConfigImport={(yaml, dryRun) => ws.clientRef.current?.orgConfigImport(yaml, dryRun)} configExportYaml={ws.configExportYaml} configImportPreview={ws.configImportPreview} configImportError={ws.configImportError}
            onSavedOrgsList={ws.handleSavedOrgsList} onSavedOrgSaveAs={ws.handleSavedOrgSaveAs} onSavedOrgCreate={ws.handleSavedOrgCreate} onSavedOrgLoad={ws.handleSavedOrgLoad} onSavedOrgDelete={ws.handleSavedOrgDelete}
            savedOrgsList={ws.savedOrgsList} activeSavedOrg={ws.activeSavedOrg} activeSavedOrgVersionAtLoad={ws.savedOrgVersionAtLoad} orgCreatePending={ws.orgCreatePending} orgCreateResult={ws.orgCreateResult} onSelectCorporate={ws.handleSelectCorporateOrg} />
        </div>
      )}
      {activePage === 'mapEditor' && (<div className="editor-page"><CollisionEditor bridge={bridgeRef.current} /></div>)}

      <main className={`main-grid${activePage !== 'office' ? ' hidden' : ''}${sidebarCollapsed ? ' sidebar-collapsed' : ''}`}>
        <section className="canvas-wrap">
          <PhaserGame bridge={bridgeRef.current} active={activePage === 'office'} />
          <button className="canvas-float-btn" onClick={() => setShowSubagents((v) => !v)} title={showSubagents ? 'Hide sub-agents' : 'Show sub-agents'}>{showSubagents ? '👥' : '👤'}</button>
          <button className="sidebar-collapse-btn" onClick={toggleSidebar} title={sidebarCollapsed ? 'Show side panel' : 'Hide side panel'} aria-label={sidebarCollapsed ? 'Show side panel' : 'Hide side panel'}><span className="collapse-glyph">{sidebarCollapsed ? '❮' : '❯'}</span></button>
        </section>
        <aside className="sidebar"><div className="sidebar-body"><div className="team-panel">
          <div className="mode-info-bar">
            <span className="mode-badge">{ws.globalExecMode === 'company' ? `${ws.globalExecMode}/${ws.globalCompanyProfile}` : globalModeLabel}</span>
            {isOrgMode ? <span className="mode-hint">Manage your team in the <b>Org</b> tab</span> : <span className="mode-hint">Switch to <b>Org</b> mode to create or manage agents</span>}
          </div>
          <div className="section-label">Offices <span className="count-badge">{offices.length}</span></div>
          <div className="office-cards">
            {offices.map((office) => {
              const deskCount = getOfficeDeskSeats(office.id).length
              const assignedCards = cards.filter(c => c.officeId === office.id)
              const otherAgents = cards.filter(c => c.officeId !== office.id && !c.isSubagent)
              return (
                <div key={office.id} className="office-card" onClick={() => bridgeRef.current.panToOffice(office.id)}>
                  <div className="office-card-header">
                    {editingOfficeName === office.id ? (<input className="office-name-input" value={officeNameDraft} onChange={e => setOfficeNameDraft(e.target.value)} onBlur={() => handleRenameOffice(office.id)} onKeyDown={e => { if (e.key === 'Enter') handleRenameOffice(office.id); if (e.key === 'Escape') setEditingOfficeName(null) }} autoFocus onClick={e => e.stopPropagation()} />) : (<><span className="office-name">{office.name}</span><button className="office-edit-btn" title="Rename" onClick={(e) => { e.stopPropagation(); setEditingOfficeName(office.id); setOfficeNameDraft(office.name) }}>✎</button></>)}
                    <span className="office-capacity">{assignedCards.length}/{deskCount}</span>
                  </div>
                  <div className="office-agents">
                    {assignedCards.map(c => (<span key={c.id} className="office-agent-chip" title={`${c.displayName} — ${c.seatId ?? 'no seat'}`} onClick={(e) => { e.stopPropagation(); selectAgent(c.id) }}>{c.displayName.slice(0, 8)}</span>))}
                    {isOrgMode && assignedCards.length < deskCount && otherAgents.length > 0 && (<select className="assign-dropdown" value="" onClick={e => e.stopPropagation()} onChange={e => { if (e.target.value) handleAssignAgent(office.id, e.target.value) }}><option value="">{t('app.moveHere')}</option>{otherAgents.map(a => (<option key={a.id} value={a.id}>{a.displayName} ({offices.find(o => o.id === (cards.find(cc => cc.id === a.id)?.officeId))?.name ?? '?'})</option>))}</select>)}
                  </div>
                </div>
              )
            })}
          </div>
          <div className="section-label">{t('app.activeAgents')} <span className="count-badge">{ws.swarmAgents.length}</span></div>
          <div className="agent-list">
            {ws.swarmAgents.map((agent) => (
              <div key={agent.agent_id} className={`agent-row ${selectedAgentId === agent.agent_id ? 'selected' : ''}`}>
                <button className="agent-row-main" onClick={() => selectAgent(agent.agent_id)}><span className={`dot ${agent.status}`} /><div className="agent-info"><span className="agent-name">{agent.name}</span><span className="agent-spec">{agent.specialties.slice(0, 2).join(' · ') || 'general'}</span></div></button>
                {isOrgMode && (ws.deletingAgentId === agent.agent_id ? <span className="agent-del" style={{ pointerEvents: 'none' }}><span className="spinner-inline" /></span> : ws.confirmDeleteId === agent.agent_id ? <span className="del-confirm"><span className="del-confirm-label">{t('app.deleteConfirm')}</span><button className="del-confirm-yes" onClick={() => { ws.setDeletingAgentId(agent.agent_id); ws.setConfirmDeleteId(null); ws.clientRef.current?.deleteAgent(agent.agent_id) }}>Yes</button><button className="del-confirm-no" onClick={() => ws.setConfirmDeleteId(null)}>No</button></span> : <button className="agent-del" title={`Remove ${agent.name}`} onClick={() => ws.setConfirmDeleteId(agent.agent_id)}>×</button>)}
              </div>
            ))}
            {ws.swarmAgents.length === 0 && (<div className="empty-state">{t('app.noAgents')}</div>)}
          </div>
          {selectedCard && (
            <div className="agent-detail">
              <div className="agent-detail-name">{selectedCard.displayName}</div>
              <div className="agent-detail-row"><span className="detail-label">{t('app.state')}</span><span className="detail-value">{selectedCard.state}</span></div>
              <div className="agent-detail-row"><span className="detail-label">{t('app.tool')}</span><span className="detail-value">{selectedCard.currentTool ?? '—'}</span></div>
              <div className="agent-detail-row"><span className="detail-label">{t('app.task')}</span><span className="detail-value">{selectedCard.taskSummary ?? '—'}</span></div>
              <div className="agent-detail-row"><span className="detail-label">{t('app.office')}</span><select className="detail-select" value={selectedCard.officeId} onChange={e => { handleAssignAgent(e.target.value, selectedCard.id) }} disabled={!isOrgMode}>{offices.map(o => <option key={o.id} value={o.id}>{o.name}</option>)}</select></div>
              <div className="agent-detail-row"><span className="detail-label">{t('app.seat')}</span><select className="detail-select" value={selectedCard.seatId ?? ''} onChange={e => { if (e.target.value) handleChangeSeat(selectedCard.id, e.target.value) }} disabled={!isOrgMode}><option value="">—</option>{selectedAgentSeats.map(s => { const label = s.id.replace(/^office-\d+-/, '').replace('-', ' ').replace(/\b\w/g, ch => ch.toUpperCase()); const taken = s.assigned && s.assignedTo !== selectedCard.id; return (<option key={s.id} value={s.id} disabled={taken}>{label}{taken ? ` (${s.assignedTo})` : s.assignedTo === selectedCard.id ? ' ✓' : ''}</option>) })}</select></div>
            </div>
          )}
          {cards.length > ws.swarmAgents.length && (<>
            <div className="section-label">{t('app.characters')}<button className="inline-btn" onClick={() => setShowSubagents((v) => !v)}>{showSubagents ? t('app.hideSub') : t('app.showSub')}</button></div>
            <div className="agent-list">{cards.filter((c) => !ws.swarmAgents.some((a) => a.agent_id === c.id)).map((card) => (<button key={card.id} className={`agent-row-simple ${selectedAgentId === card.id ? 'selected' : ''}`} onClick={() => selectAgent(card.id)}>{card.isSubagent && <span className="sub-badge">SUB</span>}<span className="agent-name">{card.displayName}</span><span className="agent-spec">{card.state}{card.currentTool ? ` · ${card.currentTool}` : ''}</span></button>))}</div>
          </>)}
        </div></div></aside>
      </main>

      {showDevTools && (
        <div className="dev-overlay">
          <div className="dev-header"><span className="dev-title">{t('app.devTools')}</span><button className="icon-btn" onClick={() => setShowDevTools(false)}>✕</button></div>
          <div className="dev-group"><div className="dev-label">{t('app.connection')}</div><div className="input-row"><input value={ws.wsUrlInput} onChange={(e) => ws.setWsUrlInput(e.target.value)} placeholder="ws://..." /><button className="send-btn" onClick={ws.applyWsUrl}>↩</button></div></div>
          <div className="dev-group">
            <div className="dev-label">{t('app.evolutionPipeline')}</div>
            <div className="evo-pipeline">{(['Trace', 'Reflect', 'Synthesize', 'Practice', 'Lifecycle'] as const).map((phase, i) => { const key = phase.toLowerCase() as keyof typeof evolutionPhases; const active = key in evolutionPhases ? evolutionPhases[key as 'trace' | 'reflect' | 'synthesize'] : false; return (<div key={phase} className="evo-phase-group">{i > 0 && <div className="evo-connector" />}<div className={`evo-node ${active ? 'active' : ''}`}><div className="evo-dot" /><span className="evo-label">{phase}</span></div></div>) })}</div>
            <div className="list">{(ws.snapshot?.skills.recent ?? []).slice(-6).reverse().map((item, idx) => (<div className="list-row" key={`${item.skill_name}-${item.timestamp}-${idx}`}><span>{item.skill_name}</span><span className="muted mono">{item.version}</span></div>))}</div>
          </div>
          <div className="dev-group">
            <div className="dev-label">{t('app.events')}<select className="inline-select" value={eventTypeFilter} onChange={(e) => setEventTypeFilter(e.target.value)}>{eventTypes.map((type) => <option key={type} value={type}>{type}</option>)}</select></div>
            <div className="event-log">{filteredEvents.slice(0, 30).map((evt) => (<div key={evt.event_id} className="log-row"><span className="log-time">{new Date(evt.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span><span className="log-type">{evt.type}</span><span className="log-agent">{evt.agent_id}</span><span className="log-data">{truncateJson(evt.data)}</span></div>))}</div>
          </div>
          {Object.keys(ws.snapshot?.channels ?? {}).length > 0 && (<div className="dev-group"><div className="dev-label">{t('app.channels')}</div>{Object.entries(ws.snapshot?.channels ?? {}).map(([name, info]) => (<div className="list-row" key={name}><span>{name}</span><span className="muted">{String((info as { last_type?: string }).last_type ?? 'idle')}</span></div>))}</div>)}
        </div>
      )}
      {ws.toastMessage && <div className={ws.toastType === 'error' ? 'toast-error' : 'toast-success'}>{ws.toastMessage}</div>}
      <MaybeExecutionPanel taskId={ws.executionPanelTaskId} sessions={ws.sessionStore.sessions} agents={ws.swarmAgents} onClose={() => ws.setExecutionPanelTaskId(null)} />
    </div>
  )
}
