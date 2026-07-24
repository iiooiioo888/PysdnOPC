import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { RoleAggregatedStatus, RoleWorkItemRow, RoleWorkItemSummary, Session } from '../types/kanban'
import { LlmConversationPanel } from './LlmConversationPanel'
import { IconClose } from '../chat/SvgIcons'

/* ── Types ─────────────────────────────────────────────────────────────── */

interface DashboardPageProps {
  sessions: Session[]
  projectId?: string
  /** WebSocket send function for runtime logs */
  sendRuntimeLogs?: (projectId: string, taskId: string) => void
  /** Ack handler registration for runtime logs responses */
  onRuntimeLogsAck?: (handler: (payload: Record<string, unknown>) => void) => () => void
  /** Real-time snapshot data */
  snapshot?: Record<string, unknown> | null
  /** Auto-refresh interval in ms (default: 5000) */
  refreshInterval?: number
}

interface AggregatedRole {
  roleKey: string
  roleId: string
  roleName: string
  aggregatedStatus: RoleAggregatedStatus
  runtimeStatus: string
  workItems: RoleWorkItemRow[]
  sessionTitle: string
  sessionTaskId: string
  /** Deliverables / artifacts collected from work items & sessions */
  deliverables: DeliverableItem[]
}

interface DeliverableItem {
  id: string
  name: string
  content?: string
  source: string
  workItemTitle?: string
  roleName?: string
  status: 'done' | 'pending'
  updatedAt?: number
}

interface DashboardStats {
  totalRoles: number
  active: number
  waiting: number
  pending: number
  done: number
  failed: number
  totalWorkItems: number
  activeWorkItems: number
  reviewWorkItems: number
  doneWorkItems: number
}

/* ── Helpers ───────────────────────────────────────────────────────────── */

const STATUS_CONFIG: Record<RoleAggregatedStatus, { label: string; color: string; icon: string }> = {
  active: { label: '執行中', color: '#f59e0b', icon: '⚡' },
  waiting: { label: '等待中', color: '#3b82f6', icon: '⏳' },
  pending: { label: '待處理', color: '#6b7280', icon: '📋' },
  done: { label: '已完成', color: '#22c55e', icon: '✅' },
  failed: { label: '失敗', color: '#ef4444', icon: '❌' },
}

const COLUMN_CONFIG: Record<string, { label: string; color: string }> = {
  'todo': { label: '待辦', color: '#6b7280' },
  'in-progress': { label: '進行中', color: '#f59e0b' },
  'in-review': { label: '審查中', color: '#8b5cf6' },
  'done': { label: '完成', color: '#22c55e' },
  'failed': { label: '失敗', color: '#ef4444' },
  'cancelled': { label: '已取消', color: '#9ca3af' },
}

/** Work item kind → Chinese label mapping */
const KIND_LABELS: Record<string, { label: string; icon: string }> = {
  'task': { label: '任務', icon: '📌' },
  'review': { label: '審查', icon: '🔍' },
  'report': { label: '報告', icon: '📊' },
  'rework': { label: '返工', icon: '🔄' },
  'plan': { label: '規劃', icon: '📐' },
  'design': { label: '設計', icon: '🎨' },
  'test': { label: '測試', icon: '🧪' },
  'deploy': { label: '部署', icon: '🚀' },
}

function kindBadge(kind?: string): { label: string; icon: string } {
  if (!kind) return { label: '', icon: '' }
  const lower = kind.toLowerCase()
  return KIND_LABELS[lower] ?? { label: kind, icon: '📄' }
}

function formatRelativeTime(ts: number): string {
  const sec = Math.floor((Date.now() - ts) / 1000)
  if (sec < 5) return '剛剛'
  if (sec < 60) return `${sec} 秒前`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min} 分鐘前`
  const hr = Math.floor(min / 60)
  if (hr < 24) return `${hr} 小時前`
  return `${Math.floor(hr / 24)} 天前`
}

function columnBadge(column: string): { label: string; color: string } {
  return COLUMN_CONFIG[column] ?? { label: column, color: '#6b7280' }
}

/* ── Sub-components ────────────────────────────────────────────────────── */

function StatsBar({ stats }: { stats: DashboardStats }) {
  return (
    <div className="dash-stats-bar">
      <div className="dash-stat-card">
        <span className="dash-stat-value">{stats.totalRoles}</span>
        <span className="dash-stat-label">角色總數</span>
      </div>
      <div className="dash-stat-card dash-stat-active">
        <span className="dash-stat-value">{stats.active}</span>
        <span className="dash-stat-label">執行中</span>
      </div>
      <div className="dash-stat-card dash-stat-waiting">
        <span className="dash-stat-value">{stats.waiting}</span>
        <span className="dash-stat-label">等待中</span>
      </div>
      <div className="dash-stat-card dash-stat-done">
        <span className="dash-stat-value">{stats.done}</span>
        <span className="dash-stat-label">已完成</span>
      </div>
      <div className="dash-stat-card dash-stat-failed">
        <span className="dash-stat-value">{stats.failed}</span>
        <span className="dash-stat-label">失敗</span>
      </div>
      <div className="dash-stat-divider" />
      <div className="dash-stat-card">
        <span className="dash-stat-value">{stats.totalWorkItems}</span>
        <span className="dash-stat-label">工作項目</span>
      </div>
      <div className="dash-stat-card dash-stat-active">
        <span className="dash-stat-value">{stats.activeWorkItems}</span>
        <span className="dash-stat-label">進行中</span>
      </div>
      <div className="dash-stat-card dash-stat-review">
        <span className="dash-stat-value">{stats.reviewWorkItems}</span>
        <span className="dash-stat-label">審查中</span>
      </div>
    </div>
  )
}

function WorkItemRow({ item, onViewConversation, onViewDeliverable }: { item: RoleWorkItemRow; onViewConversation?: (taskId: string) => void; onViewDeliverable?: (item: RoleWorkItemRow) => void }) {
  const badge = columnBadge(item.kanbanColumn)
  const kind = kindBadge(item.kind)
  const activityCount = item.activitySections
    ? item.activitySections.reduce((n, s) => n + (s.entries?.length ?? 0), 0)
    : item.progressLog.length

  return (
    <div className="dash-wi-row">
      <span className="dash-wi-dot" style={{ backgroundColor: badge.color }} />
      <span className="dash-wi-title" title={item.title}>{item.title}</span>
      <span className="dash-wi-badge" style={{ backgroundColor: `${badge.color}22`, color: badge.color }}>
        {badge.label}
      </span>
      {kind.label && (
        <span className="dash-wi-kind" title={`類型: ${kind.label}`}>{kind.icon} {kind.label}</span>
      )}
      {item.isReviewTarget && (
        <span className="dash-wi-review-tag" title="此項目正在審查中">🔍 審查</span>
      )}
      {activityCount > 0 && <span className="dash-wi-activity">📝 {activityCount}</span>}
      <span className="dash-wi-time">{formatRelativeTime(item.updatedAt)}</span>
      {item.executionTurnId && onViewConversation && (
        <button
          className="dash-wi-view-llm"
          title="查看 LLM 對話"
          onClick={(e) => { e.stopPropagation(); onViewConversation(item.executionTurnId!) }}
        >
          🧠
        </button>
      )}
      {onViewDeliverable && item.kanbanColumn === 'done' && (
        <button
          className="dash-wi-view-deliverable"
          title="查看交付物內容"
          onClick={(e) => { e.stopPropagation(); onViewDeliverable(item) }}
        >
          📦
        </button>
      )}
    </div>
  )
}

function RoleCard({ role, onViewConversation, onViewDeliverables }: { role: AggregatedRole; onViewConversation?: (taskId: string) => void; onViewDeliverables?: (role: AggregatedRole) => void }) {
  const config = STATUS_CONFIG[role.aggregatedStatus]
  const sortedItems = useMemo(
    () => [...role.workItems].sort((a, b) => b.updatedAt - a.updatedAt),
    [role.workItems],
  )
  const doneCount = role.workItems.filter(w => w.kanbanColumn === 'done').length
  const totalCount = role.workItems.length
  const progressPct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0

  // Compute kind distribution for responsibility visualization
  const kindDistribution = useMemo(() => {
    const dist: Record<string, number> = {}
    for (const wi of role.workItems) {
      const k = wi.kind?.toLowerCase() || 'task'
      dist[k] = (dist[k] || 0) + 1
    }
    return Object.entries(dist).sort((a, b) => b[1] - a[1])
  }, [role.workItems])

  const hasDeliverables = role.deliverables.length > 0

  return (
    <div className="dash-role-card">
      <div className="dash-role-header">
        <div className="dash-role-identity">
          <span className="dash-role-icon" style={{ backgroundColor: `${config.color}22` }}>
            {config.icon}
          </span>
          <div className="dash-role-names">
            <span className="dash-role-name">{role.roleName}</span>
            <span className="dash-role-id">{role.roleId}</span>
          </div>
        </div>
        <span className="dash-role-status" style={{ backgroundColor: `${config.color}18`, color: config.color }}>
          {config.label}
        </span>
      </div>

      {/* Responsibility / kind distribution */}
      {kindDistribution.length > 0 && (
        <div className="dash-role-responsibility">
          <span className="dash-role-resp-label">職責分工:</span>
          {kindDistribution.map(([k, count]) => {
            const kb = kindBadge(k)
            return (
              <span key={k} className="dash-role-resp-tag">
                {kb.icon} {kb.label || k} ×{count}
              </span>
            )
          })}
        </div>
      )}

      {/* Progress bar */}
      <div className="dash-role-progress">
        <div className="dash-role-progress-track">
          <div className="dash-role-progress-fill" style={{ width: `${progressPct}%`, backgroundColor: config.color }} />
        </div>
        <span className="dash-role-progress-label">{doneCount}/{totalCount} 完成</span>
      </div>

      {/* Work items */}
      <div className="dash-role-items">
        {sortedItems.length === 0 ? (
          <div className="dash-role-empty">尚無工作項目</div>
        ) : (
          sortedItems.slice(0, 6).map(item => (
            <WorkItemRow key={item.workItemId} item={item} onViewConversation={onViewConversation} />
          ))
        )}
        {sortedItems.length > 6 && (
          <div className="dash-role-more">+{sortedItems.length - 6} 更多...</div>
        )}
      </div>

      {/* Session source + action buttons */}
      <div className="dash-role-footer">
        <span className="dash-role-session">📂 {role.sessionTitle || role.sessionTaskId}</span>
        <div className="dash-role-actions">
          {hasDeliverables && onViewDeliverables && (
            <button
              className="dash-role-view-deliverables"
              title="查看此角色的交付物內容"
              onClick={() => onViewDeliverables(role)}
            >
              📦 交付物 ({role.deliverables.length})
            </button>
          )}
          {onViewConversation && (
            <button
              className="dash-role-view-llm"
              title="查看此角色的 LLM 對話記錄"
              onClick={() => onViewConversation(role.sessionTaskId)}
            >
              🧠 LLM 對話
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

/* ── Deliverable Viewer Panel ──────────────────────────────────────────── */

function DeliverableViewerPanel({
  title,
  deliverables,
  onClose,
}: {
  title: string
  deliverables: DeliverableItem[]
  onClose: () => void
}) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [copiedId, setCopiedId] = useState<string | null>(null)

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') onClose()
  }, [onClose])

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [handleKeyDown])

  const toggleExpanded = useCallback((id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const handleCopy = useCallback(async (item: DeliverableItem) => {
    if (!item.content) return
    try {
      await navigator.clipboard.writeText(item.content)
      setCopiedId(item.id)
      setTimeout(() => setCopiedId(null), 1500)
    } catch { /* clipboard unavailable */ }
  }, [])

  return (
    <>
      <div className="llm-panel-backdrop" onClick={onClose} />
      <div className="llm-panel deliverable-panel">
        <div className="llm-panel-header">
          <div className="llm-panel-title-row">
            <span className="llm-panel-icon">📦</span>
            <h3 className="llm-panel-title">交付物內容</h3>
            <span className="llm-panel-subtitle">{title}</span>
          </div>
          <button className="llm-panel-close" onClick={onClose} title="關閉 (Esc)">
            <IconClose />
          </button>
        </div>

        <div className="llm-panel-info-bar">
          <span className="llm-info-chip">共 {deliverables.length} 項交付物</span>
        </div>

        <div className="llm-panel-body">
          {deliverables.length === 0 ? (
            <div className="llm-panel-empty">
              <div className="llm-empty-icon">📦</div>
              <div className="llm-empty-text">尚無交付物</div>
              <div className="llm-empty-hint">當工作項目完成並產出交付物後，會顯示在這裡。</div>
            </div>
          ) : (
            <div className="deliverable-list">
              {deliverables.map(item => {
                const isExpanded = expandedIds.has(item.id)
                const isLong = (item.content?.length ?? 0) > 300
                const displayContent = item.content
                  ? (isExpanded || !isLong ? item.content : item.content.slice(0, 300) + '...')
                  : null
                return (
                  <div key={item.id} className="deliverable-card">
                    <div className="deliverable-card-header">
                      <span className="deliverable-icon">{item.status === 'done' ? '✅' : '⏳'}</span>
                      <span className="deliverable-name" title={item.name}>{item.name}</span>
                      {item.roleName && <span className="deliverable-meta">👤 {item.roleName}</span>}
                      {item.workItemTitle && <span className="deliverable-meta">📋 {item.workItemTitle}</span>}
                      <span className={`deliverable-status ${item.status}`}>
                        {item.status === 'done' ? '已完成' : '待產出'}
                      </span>
                      {item.content && (
                        <button
                          className="deliverable-copy"
                          onClick={() => handleCopy(item)}
                          title="複製內容"
                        >
                          {copiedId === item.id ? '✓ 已複製' : '📋 複製'}
                        </button>
                      )}
                    </div>
                    {displayContent ? (
                      <>
                        <pre className="deliverable-content">{displayContent}</pre>
                        {isLong && (
                          <button className="llm-msg-show-more" onClick={() => toggleExpanded(item.id)}>
                            {isExpanded ? '收合' : `展開完整內容 (${item.content!.length} 字元)`}
                          </button>
                        )}
                      </>
                    ) : (
                      <div className="deliverable-no-content">（無內容預覽）</div>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </>
  )
}

/* ── Main Component ────────────────────────────────────────────────────── */

export function DashboardPage({ sessions, projectId, sendRuntimeLogs, onRuntimeLogsAck, snapshot, refreshInterval = 5000 }: DashboardPageProps) {
  const [conversationTaskId, setConversationTaskId] = useState<string | null>(null)
  const [deliverableRole, setDeliverableRole] = useState<AggregatedRole | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number>(Date.now())
  const [isLive, setIsLive] = useState(true)
  const refreshTimer = useRef<ReturnType<typeof setInterval> | null>(null)

  // Auto-refresh timer
  useEffect(() => {
    if (isLive && refreshInterval > 0) {
      refreshTimer.current = setInterval(() => {
        setLastUpdated(Date.now())
      }, refreshInterval)
      return () => {
        if (refreshTimer.current) clearInterval(refreshTimer.current)
      }
    }
  }, [isLive, refreshInterval])

  const handleViewConversation = useCallback((taskId: string) => {
    setConversationTaskId(taskId)
  }, [])

  const handleCloseConversation = useCallback(() => {
    setConversationTaskId(null)
  }, [])

  const handleViewDeliverables = useCallback((role: AggregatedRole) => {
    setDeliverableRole(role)
  }, [])

  const handleCloseDeliverables = useCallback(() => {
    setDeliverableRole(null)
  }, [])

  const sendRequest = useCallback((pid: string, taskId: string) => {
    sendRuntimeLogs?.(pid, taskId)
  }, [sendRuntimeLogs])

  const toggleLive = useCallback(() => {
    setIsLive(prev => !prev)
  }, [])

  // Aggregate role data from all company-mode primary sessions
  const { roles, stats } = useMemo(() => {
    const aggregatedRoles: AggregatedRole[] = []
    const roleByKey = new Map<string, AggregatedRole>()

    for (const session of sessions) {
      // Only consider company-mode primary sessions with roleWorkItems
      const roleWorkItems = session.roleWorkItems ?? session.executorRoleWorkItems
      if (!roleWorkItems || Object.keys(roleWorkItems).length === 0) continue

      for (const [key, summary] of Object.entries(roleWorkItems)) {
        const existing = roleByKey.get(key)
        if (existing) {
          // Merge work items from multiple sessions
          const existingIds = new Set(existing.workItems.map(w => w.workItemId))
          for (const wi of summary.workItems) {
            if (!existingIds.has(wi.workItemId)) {
              existing.workItems.push(wi)
            }
          }
          // Upgrade status if the new one is more "active"
          const priority: RoleAggregatedStatus[] = ['active', 'waiting', 'pending', 'done', 'failed']
          if (priority.indexOf(summary.aggregatedStatus) < priority.indexOf(existing.aggregatedStatus)) {
            existing.aggregatedStatus = summary.aggregatedStatus
          }
        } else {
          const role: AggregatedRole = {
            roleKey: key,
            roleId: summary.roleId,
            roleName: summary.roleName,
            aggregatedStatus: summary.aggregatedStatus,
            runtimeStatus: summary.runtimeStatus,
            workItems: [...summary.workItems],
            sessionTitle: session.title,
            sessionTaskId: session.taskId,
            deliverables: [],
          }
          roleByKey.set(key, role)
          aggregatedRoles.push(role)
        }
      }
    }

    // Collect deliverables from session-level artifacts & work-item completion
    for (const session of sessions) {
      const roleWorkItems = session.roleWorkItems ?? session.executorRoleWorkItems
      if (!roleWorkItems) continue
      for (const [key, summary] of Object.entries(roleWorkItems)) {
        const role = roleByKey.get(key)
        if (!role) continue
        for (const wi of summary.workItems) {
          // Done work items with a completion report are treated as deliverables
          if (wi.kanbanColumn === 'done') {
            const completionContent = wi.activitySections
              ?.filter(s => s.kind === 'report' || s.kind === 'completion')
              .flatMap(s => s.entries.map(e => e.detail || e.summary || ''))
              .filter(Boolean)
              .join('\n')
            role.deliverables.push({
              id: `${wi.workItemId}-completion`,
              name: wi.title,
              content: completionContent || undefined,
              source: 'work-item',
              workItemTitle: wi.title,
              roleName: role.roleName,
              status: 'done',
              updatedAt: wi.updatedAt,
            })
          }
        }
      }
      // Session-level artifacts
      if (session.artifacts && session.artifacts.length > 0) {
        // Attribute to the first role of this session (primary owner)
        const firstKey = Object.keys(roleWorkItems)[0]
        const role = roleByKey.get(firstKey)
        if (role) {
          for (const artifact of session.artifacts) {
            role.deliverables.push({
              id: `${session.taskId}-artifact-${artifact}`,
              name: artifact.split('/').pop() || artifact,
              source: 'session-artifact',
              roleName: role.roleName,
              status: 'done',
            })
          }
        }
      }
    }

    // Sort: active first, then waiting, pending, done, failed
    const statusOrder: Record<RoleAggregatedStatus, number> = { active: 0, waiting: 1, pending: 2, done: 3, failed: 4 }
    aggregatedRoles.sort((a, b) => statusOrder[a.aggregatedStatus] - statusOrder[b.aggregatedStatus])

    // Compute stats
    const allWorkItems = aggregatedRoles.flatMap(r => r.workItems)
    const computedStats: DashboardStats = {
      totalRoles: aggregatedRoles.length,
      active: aggregatedRoles.filter(r => r.aggregatedStatus === 'active').length,
      waiting: aggregatedRoles.filter(r => r.aggregatedStatus === 'waiting').length,
      pending: aggregatedRoles.filter(r => r.aggregatedStatus === 'pending').length,
      done: aggregatedRoles.filter(r => r.aggregatedStatus === 'done').length,
      failed: aggregatedRoles.filter(r => r.aggregatedStatus === 'failed').length,
      totalWorkItems: allWorkItems.length,
      activeWorkItems: allWorkItems.filter(w => w.kanbanColumn === 'in-progress').length,
      reviewWorkItems: allWorkItems.filter(w => w.kanbanColumn === 'in-review').length,
      doneWorkItems: allWorkItems.filter(w => w.kanbanColumn === 'done').length,
    }

    return { roles: aggregatedRoles, stats: computedStats }
  }, [sessions])

  return (
    <div className="dashboard-page">
      <div className="dashboard-header">
        <div className="dash-header-left">
          <h2>👥 角色活動與分工</h2>
          <span className="dash-subtitle">公司模式下的角色工作狀態總覽</span>
        </div>
        <div className="dash-header-right">
          <button
            className={`dash-live-toggle ${isLive ? 'live' : 'paused'}`}
            onClick={toggleLive}
            title={isLive ? '點擊暫停自動刷新' : '點擊開啟自動刷新'}
          >
            <span className="dash-live-dot" />
            {isLive ? '實時' : '已暫停'}
          </button>
          <span className="dash-updated">更新: {formatRelativeTime(lastUpdated)}</span>
        </div>
      </div>

      {roles.length === 0 ? (
        <div className="dash-empty">
          <div className="dash-empty-icon">🏢</div>
          <div className="dash-empty-title">尚無公司模式活動</div>
          <div className="dash-empty-desc">
            在工作區以公司模式或組織模式啟動任務後，這裡會顯示各角色的工作分配與進度。
          </div>
        </div>
      ) : (
        <>
          <StatsBar stats={stats} />
          <div className="dash-roles-grid">
            {roles.map(role => (
              <RoleCard
                key={role.roleKey}
                role={role}
                onViewConversation={sendRuntimeLogs ? handleViewConversation : undefined}
                onViewDeliverables={handleViewDeliverables}
              />
            ))}
          </div>
        </>
      )}

      {/* Deliverable Viewer Panel */}
      {deliverableRole && (
        <DeliverableViewerPanel
          title={deliverableRole.roleName}
          deliverables={deliverableRole.deliverables}
          onClose={handleCloseDeliverables}
        />
      )}

      {/* LLM Conversation Panel */}
      {conversationTaskId && projectId && sendRuntimeLogs && (
        <LlmConversationPanel
          projectId={projectId}
          taskId={conversationTaskId}
          onClose={handleCloseConversation}
          sendRequest={sendRequest}
          onAck={onRuntimeLogsAck}
        />
      )}
    </div>
  )
}
