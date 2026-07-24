export type ThemeName = 'midnight' | 'neon' | 'paper' | 'retro' | 'terminal' | 'cozy' | 'openopc'
export type AppPage = 'office' | 'workspace' | 'org' | 'mapEditor' | 'dashboard' | 'templates' | 'settings'
export type AppExecMode = 'task' | 'company' | 'org'

export const MAX_LOG_ITEMS = 80

export const TASK_MODE_LOW_VALUE_RUNTIME_EVENTS = new Set([
  'message_start',
  'message_stop',
  'tool_call_delta',
  'status_snapshot',
  'context_usage',
  'cost_update',
  'task_ledger_updated',
  'prompt_prefix_state',
  'prompt_prefix_cache_fingerprint',
  'prefetch_started',
  'prefetch_completed',
  'prefetch_consumed',
  'durable_memory_extracted',
  'durable_memory_extraction_failed',
  'session_memory_updated',
  'session_memory_update_failed',
  'tool_batch_started',
  'tool_batch_completed',
  'permission_predicted',
  'turn_started',
  'turn_completed',
])

export const SESSION_DETAIL_REFRESH_LOW_VALUE_RUNTIME_EVENTS = new Set([
  'member_inbox_updated',
])
