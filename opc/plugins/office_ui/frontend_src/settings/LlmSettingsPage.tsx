import { useCallback, useEffect, useRef, useState } from 'react'
import type { VisualSocketClient } from '../lib/wsClient'
import type { LlmConfigPayload } from '../lib/wsClient'
import { t } from '../lib/locale'
import './llm-settings.css'

/* ── Types ─────────────────────────────────────────────────────────────── */

interface LlmSettingsPageProps {
  wsClient: VisualSocketClient | null
}

/* ── Component ─────────────────────────────────────────────────────────── */

export function LlmSettingsPage({ wsClient }: LlmSettingsPageProps) {
  const [config, setConfig] = useState<LlmConfigPayload | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(null)
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)

  // Local editable state
  const [defaultModel, setDefaultModel] = useState('')
  const [apiBase, setApiBase] = useState('')
  const [apiKeyEnv, setApiKeyEnv] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [temperature, setTemperature] = useState(0.3)
  const [maxTokens, setMaxTokens] = useState(32768)
  const [tierRouting, setTierRouting] = useState<Record<string, string>>({})
  const [degradeChain, setDegradeChain] = useState<Record<string, string>>({})
  const [roleModels, setRoleModels] = useState<Record<string, string>>({})

  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const showToast = useCallback((kind: 'ok' | 'error', text: string) => {
    setToast({ kind, text })
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3000)
  }, [])

  // Load config on mount
  useEffect(() => {
    if (!wsClient) return
    const prevHandler = (wsClient as any).handlers?.onLlmConfig
    ;(wsClient as any).handlers = {
      ...(wsClient as any).handlers,
      onLlmConfig: (payload: LlmConfigPayload) => {
        setConfig(payload)
        setDefaultModel(payload.default_model)
        setApiBase(payload.api_base)
        setApiKeyEnv(payload.api_key_env)
        setApiKey('')
        setTemperature(payload.temperature)
        setMaxTokens(payload.max_tokens)
        setTierRouting(payload.tier_routing || {})
        setDegradeChain(payload.degrade_chain || {})
        const rm: Record<string, string> = {}
        for (const role of payload.roles || []) {
          rm[role.role_id] = role.model || ''
        }
        setRoleModels(rm)
        setDirty(false)
      },
    }
    wsClient.llmConfigGet()
    return () => {
      if ((wsClient as any).handlers) {
        ;(wsClient as any).handlers.onLlmConfig = prevHandler
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
        if (payload.action === 'llm_config_set') {
          setSaving(false)
          if (payload.ok) {
            setDirty(false)
            showToast('ok', t('llm.saved', '設定已儲存'))
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
    const payload: Record<string, unknown> = {
      default_model: defaultModel,
      api_base: apiBase,
      api_key_env: apiKeyEnv,
      temperature,
      max_tokens: maxTokens,
      tier_routing: tierRouting,
      degrade_chain: degradeChain,
      role_models: roleModels,
    }
    if (apiKey.trim()) {
      payload.api_key = apiKey.trim()
    }
    wsClient.llmConfigSet(payload)
  }, [wsClient, defaultModel, apiBase, apiKeyEnv, apiKey, temperature, maxTokens, tierRouting, degradeChain, roleModels])

  const handleRefresh = useCallback(() => {
    wsClient?.llmConfigGet()
  }, [wsClient])

  const handleTestApi = useCallback(async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const response = await fetch('/api/llm/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: defaultModel,
          api_base: apiBase,
          api_key: apiKey || undefined,
        }),
      })
      const data = await response.json()
      if (data.ok) {
        setTestResult({ ok: true, message: `✅ API 連接成功！模型: ${data.model || defaultModel}` })
      } else {
        setTestResult({ ok: false, message: `❌ API 連接失敗: ${data.error || '未知錯誤'}` })
      }
    } catch (err) {
      setTestResult({ ok: false, message: `❌ 請求失敗: ${err}` })
    } finally {
      setTesting(false)
    }
  }, [defaultModel, apiBase, apiKey])

  const updateTier = useCallback((key: string, value: string) => {
    setTierRouting(prev => ({ ...prev, [key]: value }))
    markDirty()
  }, [markDirty])

  const updateDegrade = useCallback((key: string, value: string) => {
    setDegradeChain(prev => ({ ...prev, [key]: value }))
    markDirty()
  }, [markDirty])

  const updateRoleModel = useCallback((roleId: string, value: string) => {
    setRoleModels(prev => ({ ...prev, [roleId]: value }))
    markDirty()
  }, [markDirty])

  const tierKeys = ['critical', 'reasoning', 'routine', 'summary']

  return (
    <div className="llm-settings-page">
      <div className="llm-settings-header">
        <h2>🤖 {t('llm.title', 'LLM 模型設定')}</h2>
        <div className="llm-settings-actions">
          <button className="llm-btn" onClick={handleTestApi} disabled={testing}>
            {testing ? '測試中...' : '🔗 測試 API'}
          </button>
          <button className="llm-btn" onClick={handleRefresh}>{t('common.refresh', '重新整理')}</button>
          <button className="llm-btn primary" onClick={handleSave} disabled={!dirty || saving}>
            {saving ? t('common.loading', '儲存中...') : t('common.save', '儲存')}
          </button>
        </div>
      </div>

      {testResult && (
        <div className={`llm-test-result ${testResult.ok ? 'success' : 'error'}`}>
          {testResult.message}
        </div>
      )}

      {toast && (
        <div className={`llm-toast ${toast.kind}`}>{toast.text}</div>
      )}

      {!config ? (
        <div className="llm-loading">{t('common.loading', '載入中...')}</div>
      ) : (
        <div className="llm-settings-body">
          {/* Global Settings */}
          <section className="llm-section">
            <h3>{t('llm.globalSettings', '全域設定')}</h3>
            <div className="llm-form-grid">
              <label className="llm-field">
                <span>{t('llm.defaultModel', '預設模型')}</span>
                <input
                  type="text"
                  value={defaultModel}
                  onChange={e => { setDefaultModel(e.target.value); markDirty() }}
                  placeholder="openai/gpt-4o"
                />
              </label>
              <label className="llm-field">
                <span>{t('llm.apiBase', 'API Base URL')}</span>
                <input
                  type="text"
                  value={apiBase}
                  onChange={e => { setApiBase(e.target.value); markDirty() }}
                  placeholder="https://api.openai.com/v1"
                />
              </label>
              <label className="llm-field">
                <span>{t('llm.apiKeyEnv', 'API Key 環境變數')}</span>
                <input
                  type="text"
                  value={apiKeyEnv}
                  onChange={e => { setApiKeyEnv(e.target.value); markDirty() }}
                  placeholder="OPENAI_API_KEY"
                />
              </label>
              <label className="llm-field">
                <span>{t('llm.apiKey', 'API Key')} {config.api_key_set ? '✓' : ''}</span>
                <input
                  type="password"
                  value={apiKey}
                  onChange={e => { setApiKey(e.target.value); markDirty() }}
                  placeholder={config.api_key_set ? '(已設定，留空保持不變)' : '(未設定)'}
                />
              </label>
              <label className="llm-field">
                <span>{t('llm.temperature', '溫度')} ({temperature})</span>
                <input
                  type="range"
                  min="0"
                  max="2"
                  step="0.1"
                  value={temperature}
                  onChange={e => { setTemperature(parseFloat(e.target.value)); markDirty() }}
                />
              </label>
              <label className="llm-field">
                <span>{t('llm.maxTokens', '最大 Token 數')}</span>
                <input
                  type="number"
                  value={maxTokens}
                  onChange={e => { setMaxTokens(parseInt(e.target.value) || 0); markDirty() }}
                  min={1}
                />
              </label>
            </div>
          </section>

          {/* Tier Routing */}
          <section className="llm-section">
            <h3>{t('llm.tierRouting', '分層路由')}</h3>
            <p className="llm-hint">{t('llm.tierHint', '根據任務重要性選擇不同層級的模型')}</p>
            <div className="llm-form-grid">
              {tierKeys.map(tier => (
                <label key={tier} className="llm-field">
                  <span>{tier}</span>
                  <input
                    type="text"
                    value={tierRouting[tier] || ''}
                    onChange={e => updateTier(tier, e.target.value)}
                    placeholder={defaultModel}
                  />
                </label>
              ))}
            </div>
          </section>

          {/* Degrade Chain */}
          <section className="llm-section">
            <h3>{t('llm.degradeChain', '降級鏈')}</h3>
            <p className="llm-hint">{t('llm.degradeHint', '預算緊張時自動切換到較便宜的模型')}</p>
            <div className="llm-form-grid">
              {tierKeys.map(tier => (
                <label key={tier} className="llm-field">
                  <span>{tier}</span>
                  <input
                    type="text"
                    value={degradeChain[tier] || ''}
                    onChange={e => updateDegrade(tier, e.target.value)}
                    placeholder={tierRouting[tier] || defaultModel}
                  />
                </label>
              ))}
            </div>
          </section>

          {/* Per-Role Model Assignment */}
          <section className="llm-section">
            <h3>{t('llm.roleModels', '角色模型指派')}</h3>
            <p className="llm-hint">{t('llm.roleHint', '為每個角色指定使用的 LLM 模型（留空使用全域預設）')}</p>
            {(config.roles || []).length === 0 ? (
              <p className="llm-empty">{t('llm.noRoles', '尚無角色。請先在組織頁面建立角色。')}</p>
            ) : (
              <div className="llm-role-table">
                <div className="llm-role-header">
                  <span>{t('llm.roleName', '角色')}</span>
                  <span>{t('llm.roleModel', '模型')}</span>
                </div>
                {config.roles.map(role => (
                  <div key={role.role_id} className="llm-role-row">
                    <span className="llm-role-name">{role.name}</span>
                    <input
                      type="text"
                      value={roleModels[role.role_id] || ''}
                      onChange={e => updateRoleModel(role.role_id, e.target.value)}
                      placeholder={defaultModel || '(default)'}
                    />
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  )
}
