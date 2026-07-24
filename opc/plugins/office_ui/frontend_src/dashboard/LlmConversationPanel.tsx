import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { IconClose, IconChevron } from '../chat/SvgIcons'

/* ── Types ─────────────────────────────────────────────────────────────── */

interface RuntimeEvent {
  event_type: string
  payload?: Record<string, unknown>
  display_text?: string
  created_at?: string
  [key: string]: unknown
}

interface TranscriptEntry {
  message: {
    message_id?: string
    role?: string
    content?: string
    sender?: string
    sender_name?: string
    metadata?: Record<string, unknown>
    created_at?: string
    [key: string]: unknown
  }
  parts?: Array<{
    part_type?: string
    payload?: Record<string, unknown>
    [key: string]: unknown
  }>
}

interface RuntimeLogsResponse {
  project_id: string
  task_id: string
  target?: {
    task_id: string
    session_id: string
    title: string
    status: string
    role_id: string
    agent_id: string
    work_item_id: string
  }
  transcript: TranscriptEntry[]
  runtime_sessions: Array<Record<string, unknown>>
  runtime_events: RuntimeEvent[]
}

interface LlmConversationPanelProps {
  projectId: string
  taskId: string
  title?: string
  onClose: () => void
  /** WebSocket send function */
  sendRequest: (projectId: string, taskId: string) => void
  /** Ack handler registration */
  onAck?: (handler: (payload: Record<string, unknown>) => void) => () => void
}

/* ── Conversation message types ────────────────────────────────────────── */

type ConversationRole = 'system' | 'user' | 'assistant' | 'thinking' | 'tool' | 'event' | 'cost' | 'iteration'

interface ConversationMessage {
  id: string
  role: ConversationRole
  content: string
  timestamp?: number
  meta?: {
    toolName?: string
    model?: string
    tokensIn?: number
    tokensOut?: number
    iteration?: number
    eventType?: string
    /** Sub-classification for finer display */
    subType?: string
  }
}

/* ── Helpers ───────────────────────────────────────────────────────────── */

const ROLE_CONFIG: Record<ConversationRole, { label: string; color: string; icon: string; description: string }> = {
  system: { label: '系統提示', color: '#8b5cf6', icon: '⚙️', description: '系統級指令與設定' },
  user: { label: '用戶輸入', color: '#3b82f6', icon: '👤', description: '使用者發送的訊息' },
  assistant: { label: 'LLM 回應', color: '#22c55e', icon: '🤖', description: 'AI 模型生成的回覆內容' },
  thinking: { label: '思考過程', color: '#f59e0b', icon: '💭', description: '模型內部推理與思考鏈' },
  tool: { label: '工具調用', color: '#06b6d4', icon: '🔧', description: '工具執行請求與結果' },
  event: { label: '系統事件', color: '#6b7280', icon: '📡', description: '執行狀態與系統通知' },
  cost: { label: '費用統計', color: '#ec4899', icon: '💰', description: 'Token 用量與費用資訊' },
  iteration: { label: '迭代節點', color: '#a78bfa', icon: '🔁', description: '執行迭代的開始與結束標記' },
}

function formatTimestamp(ts?: number | string): string {
  if (!ts) return ''
  const date = typeof ts === 'string' ? new Date(ts) : new Date(ts)
  if (isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function truncateText(text: string, maxLen = 2000): string {
  if (text.length <= maxLen) return text
  return text.slice(0, maxLen) + `\n\n... [截斷，共 ${text.length} 字元]`
}

/* ── Build conversation from raw data ──────────────────────────────────── */

function buildConversation(data: RuntimeLogsResponse): ConversationMessage[] {
  const messages: ConversationMessage[] = []
  let seq = 0

  // Process transcript (session messages)
  for (const entry of data.transcript ?? []) {
    const msg = entry.message
    if (!msg) continue
    const role = str(msg.role || msg.sender || '').toLowerCase()
    const content = str(msg.content || '')
    if (!content.trim()) continue

    let convRole: ConversationRole = 'event'
    if (role === 'system') convRole = 'system'
    else if (role === 'user' || role === 'human') convRole = 'user'
    else if (role === 'assistant' || role === 'ai') convRole = 'assistant'

    messages.push({
      id: `transcript-${seq++}`,
      role: convRole,
      content: truncateText(content),
      timestamp: msg.created_at ? new Date(str(msg.created_at)).getTime() : undefined,
      meta: {
        model: str(msg.metadata?.model || ''),
      },
    })

    // Process parts (thinking, tool calls embedded in messages)
    for (const part of entry.parts ?? []) {
      const partType = str(part.part_type || '')
      const payload = part.payload || {}
      if (partType === 'thinking' && payload.text) {
        messages.push({
          id: `part-thinking-${seq++}`,
          role: 'thinking',
          content: truncateText(str(payload.text)),
          timestamp: msg.created_at ? new Date(str(msg.created_at)).getTime() : undefined,
        })
      } else if (partType === 'tool_call' && payload.name) {
        messages.push({
          id: `part-tool-${seq++}`,
          role: 'tool',
          content: `${payload.name}\n${str(payload.arguments ? JSON.stringify(payload.arguments, null, 2) : '')}`,
          timestamp: msg.created_at ? new Date(str(msg.created_at)).getTime() : undefined,
          meta: { toolName: str(payload.name) },
        })
      }
    }
  }

  // Process runtime events
  for (const event of data.runtime_events ?? []) {
    const eventType = str(event.event_type || event.payload?.type || '')
    const payload = (event.payload || {}) as Record<string, unknown>

    if (eventType === 'thinking_delta' || eventType === 'thinking') {
      const text = str(payload.text || payload.content || '')
      if (text.trim()) {
        messages.push({
          id: `event-thinking-${seq++}`,
          role: 'thinking',
          content: truncateText(text),
          timestamp: event.created_at ? new Date(str(event.created_at)).getTime() : undefined,
          meta: { eventType, iteration: num(payload.iteration) },
        })
      }
    } else if (eventType === 'assistant_delta' || eventType === 'assistant') {
      const text = str(payload.text || payload.content || '')
      if (text.trim()) {
        messages.push({
          id: `event-assistant-${seq++}`,
          role: 'assistant',
          content: truncateText(text),
          timestamp: event.created_at ? new Date(str(event.created_at)).getTime() : undefined,
          meta: { eventType, model: str(payload.model || ''), iteration: num(payload.iteration) },
        })
      }
    } else if (eventType === 'tool_started') {
      messages.push({
        id: `event-tool-start-${seq++}`,
        role: 'tool',
        content: `▶ 開始執行: ${str(payload.tool_name || 'tool')}\n${payload.arguments ? JSON.stringify(payload.arguments, null, 2) : ''}`,
        timestamp: event.created_at ? new Date(str(event.created_at)).getTime() : undefined,
        meta: { toolName: str(payload.tool_name || ''), eventType },
      })
    } else if (eventType === 'tool_completed') {
      const summary = str(payload.result_summary || payload.result_preview || '')
      messages.push({
        id: `event-tool-done-${seq++}`,
        role: 'tool',
        content: `✓ 完成: ${str(payload.tool_name || 'tool')}${summary ? `\n${truncateText(summary, 500)}` : ''}`,
        timestamp: event.created_at ? new Date(str(event.created_at)).getTime() : undefined,
        meta: { toolName: str(payload.tool_name || ''), eventType },
      })
    } else if (eventType === 'cost_update') {
      messages.push({
        id: `event-cost-${seq++}`,
        role: 'cost',
        content: `Token 使用: 輸入=${payload.tokens_in ?? '?'} 輸出=${payload.tokens_out ?? '?'} | 模型: ${str(payload.model || '?')} | 費用: $${payload.estimated_cost_delta ?? '?'}`,
        timestamp: event.created_at ? new Date(str(event.created_at)).getTime() : undefined,
        meta: { eventType, model: str(payload.model || ''), tokensIn: num(payload.tokens_in), tokensOut: num(payload.tokens_out) },
      })
    } else if (eventType === 'turn_started') {
      messages.push({
        id: `event-turn-${seq++}`,
        role: 'iteration',
        content: `── 迭代 ${payload.iteration ?? '?'} 開始 ──`,
        timestamp: event.created_at ? new Date(str(event.created_at)).getTime() : undefined,
        meta: { eventType, iteration: num(payload.iteration) },
      })
    } else if (eventType === 'turn_completed' || eventType === 'turn_failed') {
      const isFailed = eventType === 'turn_failed'
      messages.push({
        id: `event-turn-end-${seq++}`,
        role: 'iteration',
        content: `── 迭代 ${payload.iteration ?? '?'} ${isFailed ? '失敗' : '完成'} ──${payload.error ? `\n錯誤: ${str(payload.error)}` : ''}`,
        timestamp: event.created_at ? new Date(str(event.created_at)).getTime() : undefined,
        meta: { eventType, iteration: num(payload.iteration) },
      })
    } else if (eventType === 'status_snapshot') {
      // Skip noisy status snapshots
      continue
    } else if (eventType && payload) {
      // Generic event fallback
      const displayText = str(event.display_text || eventType)
      messages.push({
        id: `event-generic-${seq++}`,
        role: 'event',
        content: displayText,
        timestamp: event.created_at ? new Date(str(event.created_at)).getTime() : undefined,
        meta: { eventType },
      })
    }
  }

  return messages
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v != null ? String(v) : ''
}

function num(v: unknown): number | undefined {
  const n = Number(v)
  return isNaN(n) ? undefined : n
}

/* ── Sub-components ────────────────────────────────────────────────────── */

function MessageBubble({ msg, expanded, onToggle }: { msg: ConversationMessage; expanded: boolean; onToggle: () => void }) {
  const config = ROLE_CONFIG[msg.role]
  const isLong = msg.content.length > 300
  const displayContent = expanded || !isLong ? msg.content : msg.content.slice(0, 300) + '...'

  return (
    <div className={`llm-msg llm-msg-${msg.role}`}>
      <div className="llm-msg-header" onClick={isLong ? onToggle : undefined}>
        <span className="llm-msg-icon" style={{ backgroundColor: `${config.color}18` }}>
          {config.icon}
        </span>
        <span className="llm-msg-role" style={{ color: config.color }}>
          {config.label}
        </span>
        {msg.meta?.toolName && (
          <code className="llm-msg-tool-badge">{msg.meta.toolName}</code>
        )}
        {msg.meta?.model && (
          <span className="llm-msg-model">{msg.meta.model}</span>
        )}
        {msg.meta?.iteration != null && (
          <span className="llm-msg-iteration">#{msg.meta.iteration}</span>
        )}
        {msg.timestamp && (
          <span className="llm-msg-time">{formatTimestamp(msg.timestamp)}</span>
        )}
        {isLong && (
          <span className="llm-msg-expand-hint">
            <IconChevron down={expanded} />
          </span>
        )}
      </div>
      <div className={`llm-msg-body${expanded ? ' expanded' : ''}`}>
        <pre className="llm-msg-content">{displayContent}</pre>
      </div>
      {isLong && !expanded && (
        <button className="llm-msg-show-more" onClick={onToggle}>
          展開完整內容 ({msg.content.length} 字元)
        </button>
      )}
    </div>
  )
}

/* ── Filter options ────────────────────────────────────────────────────── */

type FilterType = 'all' | ConversationRole

const FILTER_OPTIONS: Array<{ value: FilterType; label: string; icon: string }> = [
  { value: 'all', label: '全部', icon: '📃' },
  { value: 'system', label: '系統提示', icon: '⚙️' },
  { value: 'user', label: '用戶輸入', icon: '👤' },
  { value: 'assistant', label: 'LLM 回應', icon: '🤖' },
  { value: 'thinking', label: '思考過程', icon: '💭' },
  { value: 'tool', label: '工具調用', icon: '🔧' },
  { value: 'event', label: '系統事件', icon: '📡' },
  { value: 'cost', label: '費用統計', icon: '💰' },
  { value: 'iteration', label: '迭代節點', icon: '🔁' },
]

/* ── Main Component ────────────────────────────────────────────────────── */

export function LlmConversationPanel({
  projectId,
  taskId,
  title,
  onClose,
  sendRequest,
  onAck,
}: LlmConversationPanelProps) {
  const [messages, setMessages] = useState<ConversationMessage[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<FilterType>('all')
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [targetInfo, setTargetInfo] = useState<RuntimeLogsResponse['target'] | null>(null)
  const bodyRef = useRef<HTMLDivElement>(null)

  // Stabilize callback refs to prevent effect re-triggers on parent re-render
  const sendRequestRef = useRef(sendRequest)
  sendRequestRef.current = sendRequest
  const onAckRef = useRef(onAck)
  onAckRef.current = onAck

  // Request data on mount / when target changes (NOT when callback identity changes)
  useEffect(() => {
    setLoading(true)
    setError(null)
    sendRequestRef.current(projectId, taskId)
  }, [projectId, taskId])

  // Handle ack responses (stable subscription — only re-subscribes when taskId changes)
  useEffect(() => {
    const ackFn = onAckRef.current
    if (!ackFn) return
    const unsubscribe = ackFn((payload) => {
      if (payload.task_id !== taskId && (payload.target as Record<string, unknown> | undefined)?.task_id !== taskId) return
      if (!payload.ok && payload.error) {
        setError(str(payload.error))
        setLoading(false)
        return
      }
      const data: RuntimeLogsResponse = {
        project_id: str(payload.project_id),
        task_id: str(payload.task_id),
        target: payload.target as RuntimeLogsResponse['target'],
        transcript: (payload.transcript || []) as TranscriptEntry[],
        runtime_sessions: (payload.runtime_sessions || []) as Array<Record<string, unknown>>,
        runtime_events: (payload.runtime_events || []) as RuntimeEvent[],
      }
      setTargetInfo(data.target ?? null)
      setMessages(buildConversation(data))
      setLoading(false)
    })
    return unsubscribe
  }, [taskId])

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight
    }
  }, [messages.length])

  const toggleExpanded = useCallback((id: string) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const filteredMessages = useMemo(() => {
    if (filter === 'all') return messages
    return messages.filter(m => m.role === filter)
  }, [messages, filter])

  // Count messages per category for filter badges
  const filterCounts = useMemo(() => {
    const counts: Record<string, number> = { all: messages.length }
    for (const m of messages) {
      counts[m.role] = (counts[m.role] || 0) + 1
    }
    return counts
  }, [messages])

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') onClose()
  }, [onClose])

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [handleKeyDown])

  return (
    <>
      <div className="llm-panel-backdrop" onClick={onClose} />
      <div className="llm-panel">
        {/* Header */}
        <div className="llm-panel-header">
          <div className="llm-panel-title-row">
            <span className="llm-panel-icon">🧠</span>
            <h3 className="llm-panel-title">LLM 對話詳情</h3>
            {targetInfo?.title && (
              <span className="llm-panel-subtitle">{targetInfo.title}</span>
            )}
            {targetInfo?.status && (
              <span className={`llm-panel-status llm-status-${targetInfo.status}`}>
                {targetInfo.status}
              </span>
            )}
          </div>
          <button className="llm-panel-close" onClick={onClose} title="關閉 (Esc)">
            <IconClose />
          </button>
        </div>

        {/* Target info bar */}
        {targetInfo && (
          <div className="llm-panel-info-bar">
            {targetInfo.role_id && <span className="llm-info-chip">角色: {targetInfo.role_id}</span>}
            {targetInfo.agent_id && <span className="llm-info-chip">Agent: {targetInfo.agent_id}</span>}
            {targetInfo.work_item_id && <span className="llm-info-chip">工作項: {targetInfo.work_item_id.slice(0, 8)}</span>}
            <span className="llm-info-chip">訊息數: {messages.length}</span>
          </div>
        )}

        {/* Filter bar */}
        <div className="llm-panel-filters">
          <span className="llm-filter-hint">篩選:</span>
          {FILTER_OPTIONS.map(opt => {
            const count = filterCounts[opt.value] ?? 0
            if (opt.value !== 'all' && count === 0) return null
            return (
              <button
                key={opt.value}
                className={`llm-filter-btn${filter === opt.value ? ' active' : ''}`}
                onClick={() => setFilter(opt.value)}
                title={opt.value !== 'all' ? ROLE_CONFIG[opt.value as ConversationRole]?.description : '顯示所有訊息'}
              >
                {opt.icon} {opt.label}
                {count > 0 && <span className="llm-filter-count">{count}</span>}
              </button>
            )
          })}
        </div>

        {/* Body */}
        <div className="llm-panel-body" ref={bodyRef}>
          {loading && (
            <div className="llm-panel-loading">
              <span className="llm-loading-spinner" />
              <span>載入 LLM 對話記錄...</span>
            </div>
          )}
          {error && (
            <div className="llm-panel-error">
              <span>❌</span>
              <span>{error}</span>
            </div>
          )}
          {!loading && !error && filteredMessages.length === 0 && (
            <div className="llm-panel-empty">
              <div className="llm-empty-icon">💬</div>
              <div className="llm-empty-text">尚無對話記錄</div>
              <div className="llm-empty-hint">
                當任務開始執行後，這裡會顯示完整的 LLM 交互對話內容。
              </div>
            </div>
          )}
          {!loading && !error && filteredMessages.length > 0 && (
            <div className="llm-conversation">
              {filteredMessages.map(msg => (
                <MessageBubble
                  key={msg.id}
                  msg={msg}
                  expanded={expandedIds.has(msg.id)}
                  onToggle={() => toggleExpanded(msg.id)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  )
}
