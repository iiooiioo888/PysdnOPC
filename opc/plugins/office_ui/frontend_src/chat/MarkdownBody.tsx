import React, { useCallback, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { IconCheck, IconCopy } from './SvgIcons'

const CODE_BLOCK_MAX_LINES = 30
const CODE_BLOCK_PEEK_LINES = 10

function CodeBlock({ className, children }: { className?: string; children?: React.ReactNode }) {
  const [copied, setCopied] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const text = String(children).replace(/\n$/, '')
  const lang = className?.replace('language-', '') || ''
  const lines = text.split('\n')
  const needsTruncation = lines.length > CODE_BLOCK_MAX_LINES
  const omittedCount = needsTruncation ? lines.length - CODE_BLOCK_PEEK_LINES * 2 : 0

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }, [text])

  return (
    <div className="code-block-wrap">
      <div className="code-block-header">
        <span className="code-block-lang">{lang || 'code'}{needsTruncation ? ` (${lines.length} lines)` : ''}</span>
        <button className="code-block-copy" onClick={handleCopy}>
          {copied ? <><IconCheck /> <span>Copied</span></> : <><IconCopy /> <span>Copy</span></>}
        </button>
      </div>
      <pre><code className={className}>
        {needsTruncation && !expanded ? (
          <>
            {lines.slice(0, CODE_BLOCK_PEEK_LINES).join('\n') + '\n'}
            <span className="code-block-omitted" onClick={() => setExpanded(true)}>
              {'... +'}{omittedCount}{' lines (click to expand)'}
            </span>
            {'\n' + lines.slice(-CODE_BLOCK_PEEK_LINES).join('\n')}
          </>
        ) : (
          text
        )}
      </code></pre>
      {needsTruncation && expanded && (
        <button className="code-block-collapse-btn" onClick={() => setExpanded(false)}>
          Collapse ({lines.length} lines)
        </button>
      )}
    </div>
  )
}

const mdComponents = {
  code({ className, children, ...props }: any) {
    const isBlock = className?.startsWith('language-')
    if (isBlock) {
      return <CodeBlock className={className}>{children}</CodeBlock>
    }
    return <code className={className} {...props}>{children}</code>
  },
}

const MSG_COLLAPSE_CHAR_THRESHOLD = 3000
const MSG_COLLAPSE_LINE_THRESHOLD = 60
const MSG_PREVIEW_CHARS = 800

function shouldCollapseContent(content: string): boolean {
  if (content.length > MSG_COLLAPSE_CHAR_THRESHOLD) return true
  let newlines = 0
  for (let i = 0; i < content.length; i++) {
    if (content[i] === '\n' && ++newlines >= MSG_COLLAPSE_LINE_THRESHOLD) return true
  }
  return false
}

function truncatePreview(content: string): string {
  const cut = content.lastIndexOf('\n', MSG_PREVIEW_CHARS)
  return content.slice(0, cut > MSG_PREVIEW_CHARS / 2 ? cut : MSG_PREVIEW_CHARS)
}

type MarkdownCollapseMode = 'auto' | 'never'

/* ── Granular error boundary for ReactMarkdown DOM reconciliation ────── *
 * React 19 can throw "removeChild" NotFoundError when ReactMarkdown's     *
 * output tree changes structure between rapid successive renders (e.g.    *
 * streaming content). This boundary catches the error and forces a clean  *
 * remount instead of crashing the entire app.                             */
class MarkdownRenderBoundary extends React.Component<
  { children: React.ReactNode; resetKey: string },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error) {
    // Log for diagnostics; the boundary auto-recovers on next content change.
    console.warn('[MarkdownRenderBoundary] Caught render error:', error.message)
  }

  componentDidUpdate(prevProps: { resetKey: string }) {
    // Auto-recover when the content identity changes (next render cycle).
    if (this.state.failed && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ failed: false })
    }
  }

  render() {
    if (this.state.failed) {
      // Render nothing for one frame; the resetKey change will recover us.
      return null
    }
    return this.props.children
  }
}

export const MarkdownBody = React.memo(function MarkdownBody({
  content,
  className = 'msg-content-agent',
  collapseMode = 'auto',
}: {
  content: string
  className?: string
  collapseMode?: MarkdownCollapseMode
}) {
  const collapsible = collapseMode !== 'never' && shouldCollapseContent(content)

  // Track user-initiated expand/collapse. The *initial* collapsed state is
  // derived synchronously from content (no useEffect) to avoid a
  // double-render that can desync React's fiber tree from the real DOM.
  const [userOverride, setUserOverride] = useState<null | boolean>(null)
  const prevCollapsibleRef = useRef(collapsible)

  // When the collapsible threshold flips (e.g. streaming crossed 3000 chars),
  // reset the user override so the new state takes effect immediately within
  // the SAME render — no useEffect → no second commit → no removeChild crash.
  if (prevCollapsibleRef.current !== collapsible) {
    prevCollapsibleRef.current = collapsible
    if (userOverride !== null) setUserOverride(null)
  }

  const collapsed = userOverride ?? collapsible
  const displayContent = collapsed ? truncatePreview(content) : content
  const lineCount = content.split('\n').length
  const charCount = content.length

  // A stable identity key that changes only when the *displayed* content
  // switches between truncated/full. This lets React cleanly remount the
  // markdown subtree instead of trying to reconcile structurally different
  // DOM trees (which triggers the removeChild NotFoundError).
  const renderKey = collapsed ? 'collapsed' : 'full'

  return (
    <div className={className}>
      <MarkdownRenderBoundary resetKey={renderKey + ':' + displayContent.length}>
        <ReactMarkdown key={renderKey} remarkPlugins={[remarkGfm]} components={mdComponents}>
          {displayContent}
        </ReactMarkdown>
      </MarkdownRenderBoundary>
      {collapsible && collapsed && (
        <button className="msg-collapse-toggle" onClick={() => setUserOverride(false)}>
          Show more ({lineCount} lines, {(charCount / 1000).toFixed(1)}k chars)
        </button>
      )}
      {collapsible && !collapsed && (
        <button className="msg-collapse-toggle" onClick={() => setUserOverride(true)}>
          Show less
        </button>
      )}
    </div>
  )
})
