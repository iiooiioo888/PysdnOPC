import { useEffect } from 'react'

/* ── Types ─────────────────────────────────────────────────────────────── */

export interface KeyboardShortcut {
  key: string
  ctrl?: boolean
  shift?: boolean
  alt?: boolean
  meta?: boolean
  handler: () => void
  description?: string
}

/* ── Hook ──────────────────────────────────────────────────────────────── */

/**
 * Hook to register keyboard shortcuts.
 * 
 * @param shortcuts - Array of keyboard shortcuts to register
 * @param enabled - Whether shortcuts are enabled (default: true)
 * 
 * @example
 * useKeyboardShortcuts([
 *   { key: 'k', ctrl: true, handler: () => console.log('Ctrl+K'), description: 'Command palette' },
 *   { key: 'Escape', handler: () => console.log('Esc'), description: 'Close modal' },
 * ])
 */
export function useKeyboardShortcuts(
  shortcuts: KeyboardShortcut[],
  enabled: boolean = true,
): void {
  useEffect(() => {
    if (!enabled) return

    const handleKeyDown = (event: KeyboardEvent) => {
      // Ignore when typing in input fields
      const target = event.target as HTMLElement
      if (
        target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.isContentEditable
      ) {
        return
      }

      for (const shortcut of shortcuts) {
        const ctrlMatch = shortcut.ctrl ? (event.ctrlKey || event.metaKey) : !event.ctrlKey && !event.metaKey
        const shiftMatch = shortcut.shift ? event.shiftKey : !event.shiftKey
        const altMatch = shortcut.alt ? event.altKey : !event.altKey
        const keyMatch = event.key.toLowerCase() === shortcut.key.toLowerCase()

        if (ctrlMatch && shiftMatch && altMatch && keyMatch) {
          event.preventDefault()
          shortcut.handler()
          break
        }
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [shortcuts, enabled])
}

/* ── Default Shortcuts ─────────────────────────────────────────────────── */

export const DEFAULT_SHORTCUTS = {
  commandPalette: { key: 'k', ctrl: true, description: '命令面板' },
  close: { key: 'Escape', description: '關閉' },
  refresh: { key: 'r', ctrl: true, description: '刷新' },
  settings: { key: ',', ctrl: true, description: '設定' },
  dashboard: { key: 'd', ctrl: true, description: '儀表盤' },
  workspace: { key: 'w', ctrl: true, description: '工作區' },
  office: { key: 'o', ctrl: true, description: '辦公室' },
  org: { key: 'g', ctrl: true, description: '組織' },
  templates: { key: 't', ctrl: true, description: '模板' },
} as const
