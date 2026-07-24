import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Session } from '../types/kanban'
import type { OrgInfoPayload } from '../types/visual'
import './role-profile.css'

/* ── Types ─────────────────────────────────────────────────────────────── */

interface RoleProfilePageProps {
  sessions: Session[]
  projectId?: string
  orgInfoData?: OrgInfoPayload | null
  sendRequest: (payload: Record<string, unknown>) => void
  onAck: (handler: (payload: Record<string, unknown>) => void) => () => void
}

interface MemoryRecord {
  memory_id: string
  scope: string
  summary: string
  details: Record<string, unknown> | string
  created_at?: string
}

interface WorkRecord {
  record_id: string
  title: string
  status: string
  collaborators: string[]
  started_at: string
  completed_at?: string | null
  duration_seconds: number
  summary: string
}

interface OrientationData {
  goals: Array<{ text: string; priority: string; progress: number }>
  capabilities: string[]
  values: string[]
}

interface PersonalityData {
  traits: Record<string, number>
  interaction_style: string
  behavior_notes: string[]
}

interface CollaborationRecord {
  partner_role_id: string
  interaction_count: number
  last_interaction_at?: string | null
  quality_score: number
  notes: string
}

interface SkillRecord {
  skill_id: string
  category: string
  skill_name: string
  level: number
  learning_goals: string[]
}

interface OutputMetrics {
  metrics_id: string
  week_label: string
  tasks_completed: number
  quality_score: number
  avg_duration: number
  rework_count: number
}

interface ResourceUsage {
  usage_id: string
  period: string
  tokens_in: number
  tokens_out: number
  cost_usd: number
  duration_seconds: number
}

interface TaskAssignment {
  assignment_id: string
  work_item_id: string
  title: string
  column: string
  priority: number
  depends_on: string[]
  blocked_reason: string
}

interface CommunicationRecord {
  comm_id: string
  comm_type: string
  title: string
  content: string
  participants: string[]
  outcome: string
  created_at: string
}

interface ProfileSections {
  memory: MemoryRecord[]
  work_records: WorkRecord[]
  orientation: OrientationData | null
  personality: PersonalityData | null
  collaboration: CollaborationRecord[]
  skills: SkillRecord[]
  output_metrics: OutputMetrics[]
  resource_usage: ResourceUsage[]
  task_assignments: TaskAssignment[]
  communications: CommunicationRecord[]
}

/* ── Section definitions ───────────────────────────────────────────────── */

const SECTIONS = [
  { id: 'memory', label: '角色記憶', icon: '🧠' },
  { id: 'work_records', label: '工作記錄', icon: '📋' },
  { id: 'orientation', label: '角色取向', icon: '🎯' },
  { id: 'personality', label: '角色性格', icon: '🎭' },
  { id: 'collaboration', label: '協作網路', icon: '🤝' },
  { id: 'skills', label: '技能圖譜', icon: '⚡' },
  { id: 'output_metrics', label: '產出分析', icon: '📈' },
  { id: 'resource_usage', label: '資源消耗', icon: '🔋' },
  { id: 'task_assignments', label: '任務佇列', icon: '📌' },
  { id: 'communications', label: '通訊決策', icon: '💬' },
] as const

const MEMORY_TABS = [
  { id: 'project', label: '專案記憶' },
  { id: 'global', label: '全域記憶' },
  { id: 'ephemeral', label: '暫時記憶' },
] as const

const TASK_COLUMNS = [
  { id: 'in_progress', label: '進行中', color: '#f59e0b' },
  { id: 'upcoming', label: '待處理', color: '#3b82f6' },
  { id: 'blocked', label: '阻塞', color: '#ef4444' },
  { id: 'done', label: '完成', color: '#22c55e' },
] as const

/* ── Main Component ────────────────────────────────────────────────────── */

export function RoleProfilePage({ sessions, projectId, orgInfoData, sendRequest, onAck }: RoleProfilePageProps) {
  const [selectedRoleId, setSelectedRoleId] = useState<string>('')
  const [sections, setSections] = useState<ProfileSections | null>(null)
  const [loading, setLoading] = useState(false)
  const [activeSection, setActiveSection] = useState<string>('memory')
  const [memoryTab, setMemoryTab] = useState<string>('project')
  const sectionRefs = useRef<Map<string, HTMLElement>>(new Map())
  const observerRef = useRef<IntersectionObserver | null>(null)

  // Stabilize callback refs to prevent effect re-triggers on parent re-render
  const sendRequestRef = useRef(sendRequest)
  sendRequestRef.current = sendRequest
  const onAckRef = useRef(onAck)
  onAckRef.current = onAck

  // Extract role list from orgInfoData.roles (primary) + sessions roleWorkItems (supplement)
  const roleList = useMemo(() => {
    const roles = new Map<string, string>()
    // Primary source: org config roles (always available when org is configured)
    if (orgInfoData?.roles) {
      for (const role of orgInfoData.roles) {
        if (role.role_id && !roles.has(role.role_id)) {
          roles.set(role.role_id, role.name || role.role_id)
        }
      }
    }
    // Supplement: active session roleWorkItems
    for (const session of sessions) {
      const items = session.roleWorkItems ?? session.executorRoleWorkItems
      if (items && typeof items === 'object') {
        for (const summary of Object.values(items)) {
          if (summary.roleId && !roles.has(summary.roleId)) {
            roles.set(summary.roleId, summary.roleName || summary.roleId)
          }
        }
      }
    }
    return Array.from(roles.entries()).map(([id, name]) => ({ id, name }))
  }, [sessions, orgInfoData?.roles])

  // Auto-select first role
  useEffect(() => {
    if (!selectedRoleId && roleList.length > 0) {
      setSelectedRoleId(roleList[0].id)
    }
  }, [roleList, selectedRoleId])

  // Register ack handler (stable — only re-subscribes on mount)
  useEffect(() => {
    const handler = (payload: Record<string, unknown>) => {
      if (payload.action === 'get_role_profile' && payload.ok) {
        setSections(payload.sections as unknown as ProfileSections)
        setLoading(false)
      }
    }
    const ackFn = onAckRef.current
    if (!ackFn) return
    return ackFn(handler)
  }, [])

  // Fetch profile data when role changes (NOT when callback identity changes)
  useEffect(() => {
    if (!selectedRoleId) return
    setLoading(true)
    setSections(null)
    sendRequestRef.current({ type: 'get_role_profile', role_id: selectedRoleId, project_id: projectId || 'default' })
  }, [selectedRoleId, projectId])

  // IntersectionObserver for scroll spy
  useEffect(() => {
    observerRef.current?.disconnect()
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setActiveSection(entry.target.id)
          }
        }
      },
      { rootMargin: '-80px 0px -60% 0px', threshold: 0.1 }
    )
    observerRef.current = observer
    for (const [, el] of sectionRefs.current) {
      observer.observe(el)
    }
    return () => observer.disconnect()
  }, [sections])

  const registerSectionRef = useCallback((id: string, el: HTMLElement | null) => {
    if (el) sectionRefs.current.set(id, el)
    else sectionRefs.current.delete(id)
  }, [])

  const scrollToSection = useCallback((id: string) => {
    const el = sectionRefs.current.get(id)
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  const filteredMemory = useMemo(() => {
    if (!sections?.memory) return []
    return sections.memory.filter(m => m.scope === memoryTab)
  }, [sections?.memory, memoryTab])

  return (
    <div className="role-profile-page">
      {/* Role selector */}
      <div className="rp-header">
        <h2 className="rp-title">🪪 角色画像</h2>
        <select
          className="rp-role-select"
          value={selectedRoleId}
          onChange={(e) => setSelectedRoleId(e.target.value)}
        >
          {roleList.length === 0 && <option value="">（無可用角色）</option>}
          {roleList.map(r => (
            <option key={r.id} value={r.id}>{r.name}</option>
          ))}
        </select>
      </div>

      {/* Section Navigation */}
      <nav className="rp-section-nav">
        {SECTIONS.map(s => (
          <button
            key={s.id}
            className={`rp-nav-item${activeSection === s.id ? ' active' : ''}`}
            onClick={() => scrollToSection(s.id)}
          >
            <span className="rp-nav-icon">{s.icon}</span>
            <span className="rp-nav-label">{s.label}</span>
          </button>
        ))}
      </nav>

      {/* Content */}
      {loading && <div className="rp-loading">載入中...</div>}
      {!loading && !selectedRoleId && <div className="rp-empty">請選擇一個角色</div>}
      {!loading && selectedRoleId && sections && (
        <div className="rp-content">
          {/* ① Role Memory */}
          <section id="memory" ref={(el) => registerSectionRef('memory', el)} className="rp-section">
            <h3 className="rp-section-title">🧠 角色記憶</h3>
            <div className="rp-memory-tabs">
              {MEMORY_TABS.map(tab => (
                <button
                  key={tab.id}
                  className={`rp-tab${memoryTab === tab.id ? ' active' : ''}`}
                  onClick={() => setMemoryTab(tab.id)}
                >
                  {tab.label}
                  <span className="rp-tab-count">{sections.memory?.filter(m => m.scope === tab.id).length ?? 0}</span>
                </button>
              ))}
            </div>
            <div className="rp-memory-list">
              {filteredMemory.length === 0 && <div className="rp-empty-hint">尚無記憶資料</div>}
              {filteredMemory.map(m => (
                <div key={m.memory_id} className="rp-memory-card">
                  <div className="rp-memory-key">{m.summary}</div>
                  <div className="rp-memory-value">{typeof m.details === 'string' ? m.details : JSON.stringify(m.details, null, 2)}</div>
                </div>
              ))}
            </div>
          </section>

          {/* ② Work Records - Timeline */}
          <section id="work_records" ref={(el) => registerSectionRef('work_records', el)} className="rp-section">
            <h3 className="rp-section-title">📋 工作記錄</h3>
            <div className="rp-timeline">
              {(sections.work_records ?? []).length === 0 && <div className="rp-empty-hint">尚無工作記錄</div>}
              {(sections.work_records ?? []).map(w => (
                <div key={w.record_id} className={`rp-timeline-item rp-status-${w.status}`}>
                  <div className="rp-timeline-dot" />
                  <div className="rp-timeline-body">
                    <div className="rp-timeline-header">
                      <span className="rp-timeline-title">{w.title}</span>
                      <span className={`rp-status-badge rp-badge-${w.status}`}>
                        {w.status === 'completed' ? '✅' : w.status === 'failed' ? '❌' : '⚡'} {w.status}
                      </span>
                    </div>
                    {w.summary && <div className="rp-timeline-summary">{w.summary}</div>}
                    <div className="rp-timeline-meta">
                      {w.duration_seconds > 0 && <span>⏱ {formatDuration(w.duration_seconds)}</span>}
                      {w.collaborators.length > 0 && <span>👥 {w.collaborators.join(', ')}</span>}
                      {w.started_at && <span>🕐 {formatTime(w.started_at)}</span>}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>

          {/* ③ Orientation - Placeholder */}
          <section id="orientation" ref={(el) => registerSectionRef('orientation', el)} className="rp-section">
            <h3 className="rp-section-title">🎯 角色取向</h3>
            {sections.orientation ? (
              <div className="rp-orientation">
                {sections.orientation.goals.length > 0 && (
                  <div className="rp-orient-block">
                    <h4>目標</h4>
                    {sections.orientation.goals.map((g, i) => (
                      <div key={i} className="rp-goal-row">
                        <span className="rp-goal-text">{g.text}</span>
                        <span className={`rp-priority rp-priority-${g.priority}`}>{g.priority}</span>
                        <div className="rp-progress-bar"><div className="rp-progress-fill" style={{ width: `${g.progress * 100}%` }} /></div>
                      </div>
                    ))}
                  </div>
                )}
                {sections.orientation.capabilities.length > 0 && (
                  <div className="rp-orient-block">
                    <h4>能力</h4>
                    <div className="rp-tags">{sections.orientation.capabilities.map((c, i) => <span key={i} className="rp-tag">{c}</span>)}</div>
                  </div>
                )}
                {sections.orientation.values.length > 0 && (
                  <div className="rp-orient-block">
                    <h4>價值觀</h4>
                    <div className="rp-tags">{sections.orientation.values.map((v, i) => <span key={i} className="rp-tag rp-tag-value">{v}</span>)}</div>
                  </div>
                )}
              </div>
            ) : <div className="rp-empty-hint">尚無取向數據，LLM 執行任務時將自動生成</div>}
          </section>

          {/* ④ Personality */}
          <section id="personality" ref={(el) => registerSectionRef('personality', el)} className="rp-section">
            <h3 className="rp-section-title">🎭 角色性格</h3>
            {sections.personality ? (
              <div className="rp-personality">
                <div className="rp-traits">
                  {Object.entries(sections.personality.traits).map(([trait, val]) => (
                    <div key={trait} className="rp-trait-row">
                      <span className="rp-trait-name">{trait}</span>
                      <div className="rp-trait-bar"><div className="rp-trait-fill" style={{ width: `${val * 100}%` }} /></div>
                      <span className="rp-trait-val">{Math.round(val * 100)}%</span>
                    </div>
                  ))}
                </div>
                {sections.personality.interaction_style && <div className="rp-style-note">互動風格：{sections.personality.interaction_style}</div>}
                {sections.personality.behavior_notes.length > 0 && (
                  <ul className="rp-behavior-notes">{sections.personality.behavior_notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
                )}
              </div>
            ) : <div className="rp-empty-hint">尚無性格數據，系統將根據互動歷史自動分析</div>}
          </section>

          {/* ⑤ Collaboration - Heat Matrix */}
          <section id="collaboration" ref={(el) => registerSectionRef('collaboration', el)} className="rp-section">
            <h3 className="rp-section-title">🤝 協作網路</h3>
            {(sections.collaboration ?? []).length > 0 ? (
              <div className="rp-collab-section">
                {/* Heat matrix */}
                <div className="rp-heat-matrix">
                  <div className="rp-heat-row rp-heat-header-row">
                    <span className="rp-heat-corner" />
                    {(sections.collaboration ?? []).map((c, i) => (
                      <span key={`h-${i}`} className="rp-heat-col-label" title={c.partner_role_id}>
                        {c.partner_role_id.length > 6 ? c.partner_role_id.slice(0, 6) + '…' : c.partner_role_id}
                      </span>
                    ))}
                  </div>
                  <div className="rp-heat-row">
                    <span className="rp-heat-row-label">互動次數</span>
                    {(sections.collaboration ?? []).map((c, i) => {
                      const maxCount = Math.max(...(sections.collaboration ?? []).map(x => x.interaction_count), 1)
                      const intensity = c.interaction_count / maxCount
                      return (
                        <span
                          key={`c-${i}`}
                          className="rp-heat-cell"
                          style={{ background: `rgba(59, 130, 246, ${0.1 + intensity * 0.8})` }}
                          title={`${c.partner_role_id}: ${c.interaction_count} 次`}
                        >
                          {c.interaction_count}
                        </span>
                      )
                    })}
                  </div>
                  <div className="rp-heat-row">
                    <span className="rp-heat-row-label">品質分數</span>
                    {(sections.collaboration ?? []).map((c, i) => {
                      const intensity = c.quality_score / 5
                      return (
                        <span
                          key={`q-${i}`}
                          className="rp-heat-cell"
                          style={{ background: `rgba(34, 197, 94, ${0.1 + intensity * 0.8})` }}
                          title={`${c.partner_role_id}: 品質 ${c.quality_score.toFixed(1)}`}
                        >
                          {c.quality_score.toFixed(1)}
                        </span>
                      )
                    })}
                  </div>
                </div>
                {/* Detail cards */}
                <div className="rp-collab-list">
                  {(sections.collaboration ?? []).map((c, i) => (
                    <div key={i} className="rp-collab-card">
                      <span className="rp-collab-partner">{c.partner_role_id}</span>
                      <span className="rp-collab-count">{c.interaction_count} 次互動</span>
                      <span className="rp-collab-score">品質 {c.quality_score.toFixed(1)}</span>
                      {c.notes && <span className="rp-collab-notes">{c.notes}</span>}
                    </div>
                  ))}
                </div>
              </div>
            ) : <div className="rp-empty-hint">尚無協作記錄，角色間互動後將自動統計</div>}
          </section>

          {/* ⑥ Skills - Category Radar */}
          <section id="skills" ref={(el) => registerSectionRef('skills', el)} className="rp-section">
            <h3 className="rp-section-title">⚡ 技能圖譜</h3>
            {(sections.skills ?? []).length > 0 ? (
              <div className="rp-skills-section">
                {/* Radar chart by category */}
                {(() => {
                  const skills = sections.skills ?? []
                  const categories = [...new Set(skills.map(s => s.category))]
                  const catAverages = categories.map(cat => {
                    const catSkills = skills.filter(s => s.category === cat)
                    const avg = catSkills.reduce((sum, s) => sum + s.level, 0) / catSkills.length
                    return { category: cat, average: avg, count: catSkills.length }
                  })
                  return (
                    <div className="rp-radar-wrap">
                      <div
                        className="rp-radar"
                        style={{
                          background: `conic-gradient(${catAverages.map((c, i) => {
                            const startDeg = (360 / catAverages.length) * i
                            const endDeg = (360 / catAverages.length) * (i + 1)
                            const alpha = 0.15 + c.average * 0.6
                            return `rgba(99, 102, 241, ${alpha}) ${startDeg}deg ${endDeg}deg`
                          }).join(', ')})`,
                        }}
                      >
                        <div className="rp-radar-inner" />
                      </div>
                      <div className="rp-radar-legend">
                        {catAverages.map(c => (
                          <div key={c.category} className="rp-radar-legend-item">
                            <span className="rp-radar-legend-dot" style={{ opacity: 0.3 + c.average * 0.7 }} />
                            <span className="rp-radar-legend-label">{c.category}</span>
                            <span className="rp-radar-legend-val">{Math.round(c.average * 100)}%</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )
                })()}
                {/* Skill detail cards */}
                <div className="rp-skills-grid">
                  {(sections.skills ?? []).map(s => (
                    <div key={s.skill_id} className="rp-skill-card">
                      <div className="rp-skill-header">
                        <span className="rp-skill-name">{s.skill_name}</span>
                        <span className={`rp-skill-cat rp-cat-${s.category}`}>{s.category}</span>
                      </div>
                      <div className="rp-skill-bar"><div className="rp-skill-fill" style={{ width: `${s.level * 100}%` }} /></div>
                      {s.learning_goals.length > 0 && <div className="rp-skill-goals">目標：{s.learning_goals.join('、')}</div>}
                    </div>
                  ))}
                </div>
              </div>
            ) : <div className="rp-empty-hint">尚無技能數據，LLM 完成任務後將自動累積技能經驗</div>}
          </section>

          {/* ⑦ Output Analytics - Mini bar chart */}
          <section id="output_metrics" ref={(el) => registerSectionRef('output_metrics', el)} className="rp-section">
            <h3 className="rp-section-title">📈 產出分析</h3>
            {(sections.output_metrics ?? []).length > 0 ? (
              <div className="rp-output-analytics">
                <div className="rp-bar-chart">
                  {(sections.output_metrics ?? []).slice(-12).map(m => (
                    <div key={m.metrics_id} className="rp-bar-col" title={`${m.week_label}: ${m.tasks_completed} 任務`}>
                      <div className="rp-bar" style={{ height: `${Math.min(100, m.tasks_completed * 10)}%` }} />
                      <span className="rp-bar-label">{m.week_label.slice(-3)}</span>
                    </div>
                  ))}
                </div>
                <div className="rp-output-summary">
                  {(() => {
                    const metrics = sections.output_metrics ?? []
                    const total = metrics.reduce((s, m) => s + m.tasks_completed, 0)
                    const avgQuality = metrics.length > 0 ? metrics.reduce((s, m) => s + m.quality_score, 0) / metrics.length : 0
                    const totalRework = metrics.reduce((s, m) => s + m.rework_count, 0)
                    const latest = metrics[metrics.length - 1]
                    const prev = metrics[metrics.length - 2]
                    const trend = latest && prev ? latest.tasks_completed - prev.tasks_completed : 0
                    return (
                      <>
                        <div className="rp-stat-card"><span className="rp-stat-value">{total}</span><span className="rp-stat-label">總完成任務</span></div>
                        <div className="rp-stat-card"><span className="rp-stat-value">{avgQuality.toFixed(1)}</span><span className="rp-stat-label">平均品質</span></div>
                        <div className="rp-stat-card"><span className="rp-stat-value">{totalRework}</span><span className="rp-stat-label">重工次數</span></div>
                        <div className="rp-stat-card">
                          <span className={`rp-stat-value rp-trend-${trend > 0 ? 'up' : trend < 0 ? 'down' : 'flat'}`}>
                            {trend > 0 ? '↑' : trend < 0 ? '↓' : '→'} {Math.abs(trend)}
                          </span>
                          <span className="rp-stat-label">趨勢</span>
                        </div>
                      </>
                    )
                  })()}
                </div>
              </div>
            ) : <div className="rp-empty-hint">尚無產出數據</div>}
          </section>

          {/* ⑧ Resource Usage - Stacked Bar Chart */}
          <section id="resource_usage" ref={(el) => registerSectionRef('resource_usage', el)} className="rp-section">
            <h3 className="rp-section-title">🔋 資源消耗</h3>
            {(sections.resource_usage ?? []).length > 0 ? (
              <div className="rp-resource-section">
                {/* Stacked bar chart */}
                <div className="rp-stacked-chart">
                  {(sections.resource_usage ?? []).slice(-12).map(r => {
                    const total = r.tokens_in + r.tokens_out
                    const maxTotal = Math.max(...(sections.resource_usage ?? []).slice(-12).map(x => x.tokens_in + x.tokens_out), 1)
                    const heightPct = Math.max(8, (total / maxTotal) * 100)
                    const inPct = total > 0 ? (r.tokens_in / total) * 100 : 50
                    return (
                      <div key={r.usage_id} className="rp-stacked-col" title={`${r.period}\n📥 ${r.tokens_in.toLocaleString()} / 📤 ${r.tokens_out.toLocaleString()}\n💰 $${r.cost_usd.toFixed(3)}`}>
                        <div className="rp-stacked-bar" style={{ height: `${heightPct}%` }}>
                          <div className="rp-stacked-in" style={{ height: `${inPct}%` }} />
                          <div className="rp-stacked-out" style={{ height: `${100 - inPct}%` }} />
                        </div>
                        <span className="rp-stacked-label">{r.period.slice(-4)}</span>
                      </div>
                    )
                  })}
                </div>
                <div className="rp-stacked-legend">
                  <span className="rp-stacked-legend-item"><span className="rp-legend-dot rp-legend-in" /> Token In</span>
                  <span className="rp-stacked-legend-item"><span className="rp-legend-dot rp-legend-out" /> Token Out</span>
                </div>
                {/* Detail rows */}
                <div className="rp-resource-list">
                  {(sections.resource_usage ?? []).slice(-6).map(r => (
                    <div key={r.usage_id} className="rp-resource-row">
                      <span className="rp-resource-period">{r.period}</span>
                      <span>📥 {r.tokens_in.toLocaleString()}</span>
                      <span>📤 {r.tokens_out.toLocaleString()}</span>
                      <span>💰 ${r.cost_usd.toFixed(3)}</span>
                      <span>⏱ {formatDuration(r.duration_seconds)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ) : <div className="rp-empty-hint">尚無資源消耗數據，LLM 執行任務時將自動記錄</div>}
          </section>

          {/* ⑨ Task Queue - 4-column kanban */}
          <section id="task_assignments" ref={(el) => registerSectionRef('task_assignments', el)} className="rp-section">
            <h3 className="rp-section-title">📌 任務佇列</h3>
            <div className="rp-kanban">
              {TASK_COLUMNS.map(col => {
                const tasks = (sections.task_assignments ?? []).filter(t => t.column === col.id)
                return (
                  <div key={col.id} className="rp-kanban-col">
                    <div className="rp-kanban-col-header" style={{ borderColor: col.color }}>
                      <span className="rp-kanban-col-title">{col.label}</span>
                      <span className="rp-kanban-col-count">{tasks.length}</span>
                    </div>
                    <div className="rp-kanban-cards">
                      {tasks.length === 0 && <div className="rp-kanban-empty">—</div>}
                      {tasks.map(t => (
                        <div key={t.assignment_id} className="rp-kanban-card">
                          <div className="rp-kanban-card-title">{t.title}</div>
                          {t.priority > 0 && <span className="rp-kanban-priority">P{t.priority}</span>}
                          {t.blocked_reason && <div className="rp-kanban-blocked">🚫 {t.blocked_reason}</div>}
                          {t.depends_on.length > 0 && <div className="rp-kanban-deps">依賴：{t.depends_on.join(', ')}</div>}
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          </section>

          {/* ⑩ Communications - Placeholder */}
          <section id="communications" ref={(el) => registerSectionRef('communications', el)} className="rp-section">
            <h3 className="rp-section-title">💬 通訊決策</h3>
            {(sections.communications ?? []).length > 0 ? (
              <div className="rp-comm-list">
                {(sections.communications ?? []).map(c => (
                  <div key={c.comm_id} className={`rp-comm-card rp-comm-${c.comm_type}`}>
                    <div className="rp-comm-header">
                      <span className="rp-comm-type-badge">{c.comm_type}</span>
                      <span className="rp-comm-title">{c.title}</span>
                    </div>
                    {c.content && <div className="rp-comm-content">{c.content}</div>}
                    <div className="rp-comm-meta">
                      {c.participants.length > 0 && <span>👥 {c.participants.join(', ')}</span>}
                      {c.outcome && <span>📝 {c.outcome}</span>}
                    </div>
                  </div>
                ))}
              </div>
            ) : <div className="rp-empty-hint">尚無通訊記錄，角色間的決策對話將顯示於此</div>}
          </section>
        </div>
      )}
    </div>
  )
}

/* ── Helpers ───────────────────────────────────────────────────────────── */

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleDateString('zh-TW', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  } catch {
    return iso
  }
}
