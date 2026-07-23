import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

/* ── Types ─────────────────────────────────────────────────────────────── */

interface BudgetData {
  total: number
  spent: number
  remaining: number
  pct: number
  role_breakdown: Record<string, number>
}

interface InsightItem {
  type: string
  severity: string
  message: string
  suggestion?: string
}

interface DashboardData {
  timestamp: number
  budget?: BudgetData
  events?: {
    total: number
    by_category: Record<string, number>
    ws_broadcasts: number
  }
  recent_events?: Array<{
    type: string
    event_type: string
    category: string
    payload: Record<string, unknown>
    timestamp: string
    event_id: string
  }>
  insights?: {
    score: number
    insight_count: number
    insights: InsightItem[]
  }
  model_router?: {
    default_model: string
    quality_hint: string
    budget_spent: number
  }
  auto_loop?: {
    total_runs: number
    success: number
    failed: number
    success_rate: number
    active_loops: number
    by_type: Record<string, number>
  }
  active_loops?: Array<{
    loop_id: string
    type: string
    task_id: string
    role: string
    status: string
    attempt: number
    max_attempts: number
    elapsed: number
  }>
}

interface RunEstimate {
  role_estimates: Array<{
    role: string
    model: string
    tier: string
    estimated_cost: number
  }>
  total_estimated_cost: number
  budget_limit: number
  budget_sufficient: boolean
  recommendations: string[]
}

interface DashboardPageProps {
  wsClient: any | null
}

/* ── Helpers ───────────────────────────────────────────────────────────── */

function formatUsd(value: number): string {
  return `$${value.toFixed(2)}`
}

function severityColor(severity: string): string {
  if (severity === 'critical') return '#ef4444'
  if (severity === 'warning') return '#f59e0b'
  return '#3b82f6'
}

function severityIcon(severity: string): string {
  if (severity === 'critical') return '🔴'
  if (severity === 'warning') return '🟠'
  return '🔵'
}

function categoryIcon(category: string): string {
  const icons: Record<string, string> = {
    company: '🏢', task: '📋', work_item: '📝', role: '👤',
    llm: '🤖', cost: '💰', budget: '🛡️', review: '✅', system: '⚙️',
  }
  return icons[category] ?? '📌'
}

function tierColor(tier: string): string {
  if (tier === 'heavy') return '#ef4444'
  if (tier === 'medium') return '#f59e0b'
  return '#22c55e'
}

/* ── Sub-components ────────────────────────────────────────────────────── */

function BudgetCard({ budget }: { budget: BudgetData }) {
  const barColor = budget.pct < 70 ? '#22c55e' : budget.pct < 90 ? '#f59e0b' : '#ef4444'
  const roleEntries = Object.entries(budget.role_breakdown).sort((a, b) => b[1] - a[1])

  return (
    <div className="dashboard-card">
      <h3>💰 預算狀態</h3>
      <div className="budget-amounts">
        <span className="budget-spent">{formatUsd(budget.spent)}</span>
        <span className="budget-sep"> / </span>
        <span className="budget-total">{formatUsd(budget.total)}</span>
        <span className="budget-pct">({budget.pct.toFixed(0)}%)</span>
      </div>
      <div className="budget-bar-track">
        <div className="budget-bar-fill" style={{ width: `${Math.min(100, budget.pct)}%`, backgroundColor: barColor }} />
      </div>
      <div className="budget-remaining">剩餘: {formatUsd(budget.remaining)}</div>
      {roleEntries.length > 0 && (
        <div className="budget-roles">
          {roleEntries.map(([role, spent]) => {
            const pct = budget.total > 0 ? (spent / budget.total) * 100 : 0
            return (
              <div key={role} className="budget-role-row">
                <span className="role-name">{role}</span>
                <div className="role-bar-track">
                  <div className="role-bar-fill" style={{ width: `${Math.min(100, pct)}%` }} />
                </div>
                <span className="role-cost">{formatUsd(spent)}</span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

function InsightsCard({ insights }: { insights: NonNullable<DashboardData['insights']> }) {
  const scoreColor = insights.score >= 80 ? '#22c55e' : insights.score >= 60 ? '#f59e0b' : '#ef4444'

  return (
    <div className="dashboard-card">
      <h3>
        📊 洞察分析
        <span className="insight-score" style={{ color: scoreColor }}>
          {insights.score.toFixed(0)}/100
        </span>
      </h3>
      {insights.insights.length === 0 ? (
        <div className="insight-empty">暫無洞察數據</div>
      ) : (
        <div className="insight-list">
          {insights.insights.map((item, i) => (
            <div key={i} className="insight-item" style={{ borderLeftColor: severityColor(item.severity) }}>
              <div className="insight-header">
                <span>{severityIcon(item.severity)}</span>
                <span className="insight-type">{item.type}</span>
              </div>
              <div className="insight-message">{item.message}</div>
              {item.suggestion && <div className="insight-suggestion">→ {item.suggestion}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function EventsCard({ events }: { events: NonNullable<DashboardData['events']> }) {
  const categories = Object.entries(events.by_category).sort((a, b) => b[1] - a[1])

  return (
    <div className="dashboard-card">
      <h3>📡 事件統計</h3>
      <div className="events-total">總事件數: <b>{events.total}</b></div>
      <div className="events-categories">
        {categories.map(([cat, count]) => (
          <div key={cat} className="event-cat-row">
            <span className="cat-icon">{categoryIcon(cat)}</span>
            <span className="cat-name">{cat}</span>
            <span className="cat-count">{count}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function RecentEventsCard({ events }: { events: NonNullable<DashboardData['recent_events']> }) {
  return (
    <div className="dashboard-card">
      <h3>📋 最近事件</h3>
      <div className="recent-events-list">
        {events.slice(-15).reverse().map((evt) => (
          <div key={evt.event_id} className="recent-event-row">
            <span className="evt-icon">{categoryIcon(evt.category)}</span>
            <span className="evt-type">{evt.event_type}</span>
            <span className="evt-time">{evt.timestamp?.slice(11, 19) ?? ''}</span>
          </div>
        ))}
        {events.length === 0 && <div className="events-empty">暫無事件</div>}
      </div>
    </div>
  )
}

function ModelRouterCard({ router }: { router: NonNullable<DashboardData['model_router']> }) {
  return (
    <div className="dashboard-card">
      <h3>🧠 模型路由</h3>
      <div className="router-info">
        <div className="router-row">
          <span className="router-label">預設模型</span>
          <span className="router-value">{router.default_model}</span>
        </div>
        <div className="router-row">
          <span className="router-label">品質偏好</span>
          <span className="router-value">{router.quality_hint}</span>
        </div>
        <div className="router-row">
          <span className="router-label">已花費</span>
          <span className="router-value">{formatUsd(router.budget_spent)}</span>
        </div>
      </div>
    </div>
  )
}

function AutoLoopCard({ stats, activeLoops }: {
  stats: NonNullable<DashboardData['auto_loop']>
  activeLoops: NonNullable<DashboardData['active_loops']>
}) {
  const rateColor = stats.success_rate >= 80 ? '#22c55e' : stats.success_rate >= 50 ? '#f59e0b' : '#ef4444'
  const typeNames: Record<string, string> = {
    retry: '🔁 重試', self_heal: '🩹 自癒', quality_gate: '✅ 質量門禁',
    watchdog: '🐕 看門狗', improvement: '📈 改進',
  }

  return (
    <div className="dashboard-card">
      <h3>🔄 自動循環</h3>
      <div className="loop-stats">
        <div className="loop-stat-main">
          <span className="loop-rate" style={{ color: rateColor }}>{stats.success_rate.toFixed(0)}%</span>
          <span className="loop-rate-label">成功率</span>
        </div>
        <div className="loop-stat-detail">
          <span>✅ {stats.success} 成功</span>
          <span>❌ {stats.failed} 失敗</span>
          <span>🔄 {stats.active_loops} 活動中</span>
        </div>
      </div>
      {Object.keys(stats.by_type).length > 0 && (
        <div className="loop-types">
          {Object.entries(stats.by_type).map(([type, count]) => (
            <div key={type} className="loop-type-row">
              <span>{typeNames[type] ?? type}</span>
              <span className="loop-type-count">{count}</span>
            </div>
          ))}
        </div>
      )}
      {activeLoops.length > 0 && (
        <div className="active-loops">
          <h4>活動循環</h4>
          {activeLoops.map(loop => (
            <div key={loop.loop_id} className="active-loop-row">
              <span className="loop-status-dot" style={{ backgroundColor: loop.status === 'running' ? '#f59e0b' : '#22c55e' }} />
              <span className="loop-task">{loop.task_id}</span>
              <span className="loop-attempt">{loop.attempt}/{loop.max_attempts}</span>
              <span className="loop-elapsed">{loop.elapsed.toFixed(0)}s</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function EstimatePanel({ wsClient }: { wsClient: any | null }) {
  const [task, setTask] = useState('')
  const [budget, setBudget] = useState('3.0')
  const [estimate, setEstimate] = useState<RunEstimate | null>(null)
  const [loading, setLoading] = useState(false)

  const handleEstimate = useCallback(() => {
    if (!task.trim()) return
    setLoading(true)
    // Use WebSocket to request estimate from backend
    if (wsClient) {
      wsClient.send(JSON.stringify({
        action: 'estimate_cost',
        task: task,
        budget: parseFloat(budget) || 0,
      }))
    }
    // Simulate for now
    setTimeout(() => {
      setEstimate({
        role_estimates: [
          { role: 'manager', model: 'gpt-4o', tier: 'heavy', estimated_cost: 0.45 },
          { role: 'researcher', model: 'gpt-4o-mini', tier: 'medium', estimated_cost: 0.25 },
          { role: 'writer', model: 'gpt-4o-mini', tier: 'medium', estimated_cost: 0.20 },
        ],
        total_estimated_cost: 0.90,
        budget_limit: parseFloat(budget) || 0,
        budget_sufficient: true,
        recommendations: [],
      })
      setLoading(false)
    }, 500)
  }, [task, budget, wsClient])

  return (
    <div className="dashboard-card estimate-panel">
      <h3>💰 成本估算</h3>
      <div className="estimate-form">
        <input
          className="estimate-input"
          placeholder="描述你的任務..."
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleEstimate()}
        />
        <div className="estimate-controls">
          <input
            className="estimate-budget"
            type="number"
            placeholder="預算"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            step="0.5"
            min="0"
          />
          <button className="estimate-btn" onClick={handleEstimate} disabled={loading || !task.trim()}>
            {loading ? '⏳' : '📊'} 估算
          </button>
        </div>
      </div>
      {estimate && (
        <div className="estimate-result">
          <div className="estimate-roles">
            {estimate.role_estimates.map((e) => (
              <div key={e.role} className="estimate-role-row">
                <span className="role-dot" style={{ backgroundColor: tierColor(e.tier) }} />
                <span className="role-name">{e.role}</span>
                <span className="role-model">{e.model}</span>
                <span className="role-cost">{formatUsd(e.estimated_cost)}</span>
              </div>
            ))}
          </div>
          <div className="estimate-total">
            預估總費用: <b>{formatUsd(estimate.total_estimated_cost)}</b>
            {estimate.budget_limit > 0 && (
              <span className="estimate-budget-info">
                {' '}/ {formatUsd(estimate.budget_limit)}
                {estimate.budget_sufficient ? ' ✅' : ' ⚠️ 超出預算'}
              </span>
            )}
          </div>
          {estimate.recommendations.length > 0 && (
            <div className="estimate-recs">
              {estimate.recommendations.map((r, i) => <div key={i} className="estimate-rec">{r}</div>)}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ── Main Component ────────────────────────────────────────────────────── */

export function DashboardPage({ wsClient }: DashboardPageProps) {
  const [data, setData] = useState<DashboardData | null>(null)
  const [connected, setConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Connect to dashboard WebSocket
  useEffect(() => {
    const wsProto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const wsUrl = `${wsProto}://${window.location.hostname}:8766`

    let ws: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null

    const connect = () => {
      try {
        ws = new WebSocket(wsUrl)
        wsRef.current = ws

        ws.onopen = () => {
          setConnected(true)
          // Request initial data
          ws?.send(JSON.stringify({ type: 'get_status' }))
        }

        ws.onmessage = (evt) => {
          try {
            const msg = JSON.parse(evt.data)
            if (msg.type === 'init' || msg.type === 'status') {
              setData(msg.data)
            } else if (msg.type === 'event') {
              // Real-time event — refresh data
              setData(prev => prev ? {
                ...prev,
                recent_events: [...(prev.recent_events ?? []).slice(-49), msg],
              } : prev)
            }
          } catch { /* ignore parse errors */ }
        }

        ws.onclose = () => {
          setConnected(false)
          reconnectTimer = setTimeout(connect, 3000)
        }

        ws.onerror = () => {
          ws?.close()
        }
      } catch { /* ignore connection errors */ }
    }

    connect()

    // Fallback: poll via main WebSocket if dashboard WS unavailable
    pollRef.current = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: 'get_status' }))
      }
    }, 5000)

    return () => {
      if (reconnectTimer) clearTimeout(reconnectTimer)
      if (pollRef.current) clearInterval(pollRef.current)
      ws?.close()
    }
  }, [])

  const handleRefresh = useCallback(() => {
    wsRef.current?.send(JSON.stringify({ type: 'get_status' }))
  }, [])

  return (
    <div className="dashboard-page">
      <div className="dashboard-header">
        <h2>📊 儀表盤</h2>
        <div className="dashboard-actions">
          <span className={`ws-status ${connected ? 'connected' : 'disconnected'}`}>
            {connected ? '🟢 已連接' : '🔴 未連接'}
          </span>
          <button className="refresh-btn" onClick={handleRefresh}>🔄 刷新</button>
        </div>
      </div>

      <div className="dashboard-grid">
        {/* Row 1: Budget + Model Router */}
        {data?.budget && data.budget.total > 0 && (
          <BudgetCard budget={data.budget} />
        )}
        {data?.model_router && (
          <ModelRouterCard router={data.model_router} />
        )}

        {/* Row 2: Insights + Auto Loop */}
        {data?.insights && (
          <InsightsCard insights={data.insights} />
        )}
        {data?.auto_loop && (
          <AutoLoopCard stats={data.auto_loop} activeLoops={data.active_loops ?? []} />
        )}

        {/* Row 3: Events */}
        {data?.events && (
          <EventsCard events={data.events} />
        )}

        {/* Row 4: Recent Events */}
        {data?.recent_events && data.recent_events.length > 0 && (
          <RecentEventsCard events={data.recent_events} />
        )}

        {/* Row 5: Cost Estimator */}
        <EstimatePanel wsClient={wsClient} />
      </div>
    </div>
  )
}
