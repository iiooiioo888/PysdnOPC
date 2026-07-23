import { useCallback, useEffect, useMemo, useState } from 'react'

/* ── Types ─────────────────────────────────────────────────────────────── */

interface TemplateRole {
  id: string
  name: string
  description: string
  model_tier: string
}

interface TemplateInfo {
  id: string
  name: string
  description: string
  roles: TemplateRole[]
  talent_templates: number
}

interface TemplatesPageProps {
  onApplyTemplate?: (templateId: string) => void
}

/* ── Helpers ───────────────────────────────────────────────────────────── */

function tierBadge(tier: string): { label: string; color: string } {
  if (tier === 'heavy') return { label: '重型', color: '#ef4444' }
  if (tier === 'medium') return { label: '中型', color: '#f59e0b' }
  return { label: '輕型', color: '#22c55e' }
}

function domainIcon(domain: string): string {
  const icons: Record<string, string> = {
    finance: '📈', dev: '💻', content: '🎬', data: '📊',
    design: '🎨', marketing: '📢', education: '🎓', legal: '⚖️',
    general: '📋',
  }
  return icons[domain] ?? '📋'
}

/* ── Main Component ────────────────────────────────────────────────────── */

export function TemplatesPage({ onApplyTemplate }: TemplatesPageProps) {
  const [templates, setTemplates] = useState<TemplateInfo[]>([])
  const [search, setSearch] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // Load templates from backend API
  useEffect(() => {
    setLoading(true)
    fetch('/api/org_templates')
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(data => {
        setTemplates(Array.isArray(data.templates) ? data.templates : [])
        setLoading(false)
      })
      .catch(() => {
        // Fallback: use static template list
        setTemplates([
          {
            id: 'finance/research_report', name: '金融研究團隊',
            description: '端到端金融研究報告團隊，從桌面研究到報告交付',
            roles: [
              { id: 'manager', name: '項目經理', description: '制定研究計劃，協調團隊', model_tier: 'heavy' },
              { id: 'researcher', name: '行業研究員', description: '桌面研究，收集行業數據', model_tier: 'medium' },
              { id: 'analyst', name: '數據分析師', description: '財務數據分析、估值建模', model_tier: 'medium' },
              { id: 'writer', name: '報告撰寫', description: '撰寫結構化報告', model_tier: 'medium' },
            ],
            talent_templates: 3,
          },
          {
            id: 'dev/fullstack_app', name: '全棧開發團隊',
            description: '端到端軟件開發團隊，從需求分析到測試部署',
            roles: [
              { id: 'architect', name: '架構師', description: '技術選型、架構設計', model_tier: 'heavy' },
              { id: 'developer', name: '開發工程師', description: '核心代碼實現', model_tier: 'medium' },
              { id: 'reviewer', name: '代碼審查員', description: '代碼質量審查', model_tier: 'medium' },
            ],
            talent_templates: 2,
          },
          {
            id: 'content/article_series', name: '內容製作團隊',
            description: '端到端內容製作團隊，從選題到交付',
            roles: [
              { id: 'manager', name: '內容策劃', description: '選題策劃、質量把控', model_tier: 'heavy' },
              { id: 'researcher', name: '素材研究員', description: '收集素材、數據、案例', model_tier: 'medium' },
              { id: 'writer', name: '內容撰寫', description: '撰寫文章、腳本', model_tier: 'medium' },
              { id: 'designer', name: '視覺設計', description: '配圖、排版', model_tier: 'light' },
            ],
            talent_templates: 2,
          },
          {
            id: 'general/quick_task', name: '快速任務團隊',
            description: '精簡團隊，快速完成單一任務',
            roles: [
              { id: 'executor', name: '執行者', description: '獨立完成任務', model_tier: 'medium' },
            ],
            talent_templates: 1,
          },
          {
            id: 'general/deep_research', name: '深度研究團隊',
            description: '多角色協作的深度研究團隊',
            roles: [
              { id: 'manager', name: '研究總監', description: '制定研究框架', model_tier: 'heavy' },
              { id: 'researcher_primary', name: '主研究員', description: '核心維度深度研究', model_tier: 'medium' },
              { id: 'researcher_secondary', name: '輔助研究員', description: '補充維度研究', model_tier: 'medium' },
              { id: 'analyst', name: '分析師', description: '數據分析和洞察', model_tier: 'medium' },
              { id: 'writer', name: '報告撰寫', description: '整合為最終報告', model_tier: 'medium' },
            ],
            talent_templates: 2,
          },
        ])
        setLoading(false)
      })
  }, [])

  const filtered = useMemo(() => {
    if (!search.trim()) return templates
    const q = search.toLowerCase()
    return templates.filter(t =>
      t.id.toLowerCase().includes(q) ||
      t.name.toLowerCase().includes(q) ||
      t.description.toLowerCase().includes(q)
    )
  }, [templates, search])

  const selected = useMemo(() =>
    templates.find(t => t.id === selectedId) ?? null,
    [templates, selectedId]
  )

  const handleApply = useCallback((templateId: string) => {
    if (onApplyTemplate) {
      onApplyTemplate(templateId)
    }
  }, [onApplyTemplate])

  return (
    <div className="templates-page">
      <div className="templates-header">
        <h2>🏗️ 組織模板</h2>
        <div className="templates-search">
          <input
            className="search-input"
            placeholder="搜索模板..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      {loading ? (
        <div className="templates-loading">⏳ 載入中...</div>
      ) : (
        <div className="templates-content">
          {/* Template List */}
          <div className="template-list">
            {filtered.length === 0 ? (
              <div className="template-empty">未找到匹配的模板</div>
            ) : (
              filtered.map(t => {
                const domain = t.id.split('/')[0] ?? 'general'
                return (
                  <div
                    key={t.id}
                    className={`template-card ${selectedId === t.id ? 'selected' : ''}`}
                    onClick={() => setSelectedId(t.id)}
                  >
                    <div className="template-card-header">
                      <span className="template-icon">{domainIcon(domain)}</span>
                      <span className="template-name">{t.name}</span>
                      <span className="template-roles-count">{t.roles.length} 角色</span>
                    </div>
                    <div className="template-id">{t.id}</div>
                    <div className="template-desc">{t.description}</div>
                  </div>
                )
              })
            )}
          </div>

          {/* Template Detail */}
          {selected && (
            <div className="template-detail">
              <div className="detail-header">
                <h3>{domainIcon(selected.id.split('/')[0])} {selected.name}</h3>
                <button className="apply-btn" onClick={() => handleApply(selected.id)}>
                  🚀 應用模板
                </button>
              </div>
              <div className="detail-desc">{selected.description}</div>
              <div className="detail-meta">
                <span>📋 {selected.roles.length} 個角色</span>
                <span>👤 {selected.talent_templates} 個人才模板</span>
              </div>

              <h4>角色配置</h4>
              <div className="detail-roles">
                {selected.roles.map(role => {
                  const badge = tierBadge(role.model_tier)
                  return (
                    <div key={role.id} className="detail-role-card">
                      <div className="role-header">
                        <span className="role-id">{role.id}</span>
                        <span className="tier-badge" style={{ backgroundColor: badge.color }}>
                          {badge.label}
                        </span>
                      </div>
                      <div className="role-name">{role.name}</div>
                      <div className="role-desc">{role.description}</div>
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
