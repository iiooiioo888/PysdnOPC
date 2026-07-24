import { useCallback, useEffect, useRef, useState } from 'react'
import type { VisualSocketClient } from '../lib/wsClient'
import type { AgentConfigEntry, AgentConfigPayload } from '../lib/wsClient'
import { t } from '../lib/locale'
import './agent-settings.css'

/* ── Types ─────────────────────────────────────────────────────────────── */

interface AgentSettingsPageProps {
  wsClient: VisualSocketClient | null
}

/* ── Component ─────────────────────────────────────────────────────────── */

export function AgentSettingsPage({ wsClient }: AgentSettingsPageProps) {
  const [config, setConfig] = useState<AgentConfigPayload | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)

  // Local editable state
  const [agents, setAgents] = useState<Record<string, AgentConfigEntry>>({})
  const [preferredOrder, setPreferredOrder] = useState<string[]>([])

  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const showToast = useCallback((kind: 'ok' | 'error', text: string) => {
    setToast({ kind, text })
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3000)
  }, [])

  // Load config on mount
  useEffect(() => {
    if (!wsClient) return
    const prevHandler = (wsClient as any).handlers?.onAgentConfig
    ;(wsClient as any).handlers = {
      ...(wsClient as any).handlers,
      onAgentConfig: (payload: AgentConfigPayload) => {
        setConfig(payload)
        setAgents(payload.agents || {})
        setPreferredOrder(payload.preferred_order || [])
        setDirty(false)
      },
    }
    wsClient.agentConfigGet()
    return () => {
      if ((wsClient as any).handlers) {
        ;(wsClient as any).handlers.onAgentConfig = prevHandler
      }
    }
  }, [wsClient])

  // Listen for ack
  useEffect(() => {
    if (!wsClient) return
    const prevAck = (wsClient as any).handlers?.onAck
    ;(wsClient as any).handlers = {
      ...(wsClient as any).handlers,
      onAck: (payload: Record<string, unknown>) => {
        if (payload.action === 'agent_config_set') {
          setSaving(false)
          if (payload.ok) {
            setDirty(false)
            showToast('ok', t('agent.saved', '代理設定已儲存'))
          } else {
            showToast('error', String(payload.error || 'Failed to save'))
          }
        }
        prevAck?.(payload)
      },
    }
    return () => {
      if ((wsClient as any).handlers) {
        ;(wsClient as any).handlers.onAck = prevAck
      }
    }
  }, [wsClient, showToast])

  const markDirty = useCallback(() => setDirty(true), [])

  const handleSave = useCallback(() => {
    if (!wsClient) return
    setSaving(true)
    wsClient.agentConfigSet({
      agents,
      preferred_order: preferredOrder,
    })
  }, [wsClient, agents, preferredOrder])

  const handleRefresh = useCallback(() => {
    wsClient?.agentConfigGet()
  }, [wsClient])

  const updateAgent = useCallback((agentId: string, field: string, value: unknown) => {
    setAgents(prev => ({
      ...prev,
      [agentId]: { ...prev[agentId], [field]: value },
    }))
    markDirty()
  }, [markDirty])

  const toggleAgent = useCallback((agentId: string) => {
    setAgents(prev => ({
      ...prev,
      [agentId]: { ...prev[agentId], enabled: !prev[agentId]?.enabled },
    }))
    markDirty()
  }, [markDirty])

  const moveOrder = useCallback((index: number, direction: -1 | 1) => {
    setPreferredOrder(prev => {
      const next = [...prev]
      const target = index + direction
      if (target < 0 || target >= next.length) return prev
      ;[next[index], next[target]] = [next[target], next[index]]
      return next
    })
    markDirty()
  }, [markDirty])

  // Agent display names
  const AGENT_LABELS: Record<string, string> = {
    qwen_code: 'Qwen Code',
    codex: 'Codex',
    claude_code: 'Claude Code',
    cursor: 'Cursor',
    opencode: 'OpenCode',
  }

  return (
    <div className="agent-settings-page">
      <div className="agent-settings-header">
        <h2>🔧 {t('agent.title', '外部代理設定')}</h2>
        <div className="agent-settings-actions">
          <button className="agent-btn" onClick={handleRefresh}>{t('common.refresh', '重新整理')}</button>
          <button className="agent-btn primary" onClick={handleSave} disabled={!dirty || saving}>
            {saving ? t('common.loading', '儲存中...') : t('common.save', '儲存')}
          </button>
        </div>
      </div>

      {toast && (
        <div className={`agent-toast ${toast.kind}`}>{toast.text}</div>
      )}

      {!config ? (
        <div className="llm-loading">{t('common.loading', '載入中...')}</div>
      ) : (
        <div className="agent-settings-body">
          {/* Preferred Order */}
          <div className="agent-preferred-order">
            <h3>{t('agent.preferredOrder', '優先順序')}</h3>
            <p>{t('agent.orderHint', '當多個代理可用時，依此順序選擇。拖拽或按鈕調整順序。')}</p>
            <div className="agent-order-list">
              {preferredOrder.map((agentId, idx) => (
                <div key={agentId} className="agent-order-item">
                  <span className="order-num">{idx + 1}</span>
                  <span className="order-name">{AGENT_LABELS[agentId] || agentId}</span>
                  <div className="order-btns">
                    <button onClick={() => moveOrder(idx, -1)} disabled={idx === 0} title="上移">↑</button>
                    <button onClick={() => moveOrder(idx, 1)} disabled={idx === preferredOrder.length - 1} title="下移">↓</button>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Agent Cards */}
          {Object.entries(agents).map(([agentId, agent]) => (
            <div key={agentId} className={`agent-card${agent.enabled ? ' enabled' : ''}`}>
              <div className="agent-card-header">
                <h3>{AGENT_LABELS[agentId] || agentId}</h3>
                <button
                  className={`agent-toggle${agent.enabled ? ' on' : ''}`}
                  onClick={() => toggleAgent(agentId)}
                  title={agent.enabled ? t('agent.disable', '禁用') : t('agent.enable', '啟用')}
                />
              </div>
              <div className="agent-card-body">
                <label className="agent-field">
                  <span>{t('agent.command', '命令')}</span>
                  <input
                    type="text"
                    value={agent.command || ''}
                    onChange={e => updateAgent(agentId, 'command', e.target.value)}
                    placeholder="e.g. claude, codex"
                  />
                </label>
                <label className="agent-field">
                  <span>{t('agent.model', '模型')}</span>
                  <input
                    type="text"
                    value={agent.model || ''}
                    onChange={e => updateAgent(agentId, 'model', e.target.value)}
                    placeholder="(留空使用預設)"
                  />
                </label>
                <label className="agent-field">
                  <span>{t('agent.authType', '認證類型')}</span>
                  <select
                    value={agent.auth_type || ''}
                    onChange={e => updateAgent(agentId, 'auth_type', e.target.value)}
                  >
                    <option value="">(auto)</option>
                    <option value="openai">openai</option>
                    <option value="api_key">api_key</option>
                    <option value="oauth">oauth</option>
                  </select>
                </label>
                <label className="agent-field">
                  <span>{t('agent.runMode', '執行模式')}</span>
                  <select
                    value={agent.run_mode || 'interactive'}
                    onChange={e => updateAgent(agentId, 'run_mode', e.target.value)}
                  >
                    <option value="interactive">interactive</option>
                    <option value="batch">batch</option>
                  </select>
                </label>
                <label className="agent-field">
                  <span>{t('agent.approvalMode', '審批模式')}</span>
                  <select
                    value={agent.approval_mode || 'auto'}
                    onChange={e => updateAgent(agentId, 'approval_mode', e.target.value)}
                  >
                    <option value="auto">auto</option>
                    <option value="full-auto">full-auto</option>
                    <option value="manual">manual</option>
                  </select>
                </label>
                <label className="agent-field">
                  <span>{t('agent.timeout', '互動逾時 (秒)')}</span>
                  <input
                    type="number"
                    value={agent.interactive_timeout_seconds || 21600}
                    onChange={e => updateAgent(agentId, 'interactive_timeout_seconds', parseInt(e.target.value) || 21600)}
                    min={60}
                  />
                </label>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
