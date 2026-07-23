import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { RoleAggregatedStatus, RoleWorkItemRow, RoleWorkItemSummary, Session } from '../types/kanban'
import { LlmConversationPanel } from './LlmConversationPanel'

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

function WorkItemRow({ item, onViewConversation }: { item: RoleWorkItemRow; onViewConversation?: (taskId: string) => void }) {
  const badge = columnBadge(item.kanbanColumn)
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
      {item.kind && <span className="dash-wi-kind">{item.kind}</span>}
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
    </div>
  )
}

function RoleCard({ role, onViewConversation }: { role: AggregatedRole; onViewConversation?: (taskId: string) => void }) {
  const config = STATUS_CONFIG[role.aggregatedStatus]
  const sortedItems = useMemo(
    () => [...role.workItems].sort((a, b) => b.updatedAt - a.updatedAt),
    [role.workItems],
  )
  const doneCount = role.workItems.filter(w => w.kanbanColumn === 'done').length
  const totalCount = role.workItems.length
  const progressPct = totalCount > 0 ? Math.round((doneCount / totalCount) * 100) : 0

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

      {/* Session source + LLM conversation button */}
      <div className="dash-role-footer">
        <span className="dash-role-session">📂 {role.sessionTitle || role.sessionTaskId}</span>
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
  )
}

/* ── Main Component ────────────────────────────────────────────────────── */

export function DashboardPage({ sessions, projectId, sendRuntimeLogs, onRuntimeLogsAck, snapshot, refreshInterval = 5000 }: DashboardPageProps) {
  const [conversationTaskId, setConversationTaskId] = useState<string | null>(null)
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
          }
          roleByKey.set(key, role)
          aggregatedRoles.push(role)
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
              />
            ))}
          </div>
        </>
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
