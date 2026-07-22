import assert from 'node:assert/strict'
import type { Session } from '../types/kanban'
import {
  composerExecModeForSession,
  conversationHasOlderHistory,
  sessionHasMoreForDetail,
} from './ContextPanel'

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    projectId: 'default',
    taskId: 'task-1',
    channelId: 'session:task-1',
    title: 'Task',
    status: 'running',
    columnId: 'in-progress',
    assigneeIds: [],
    priority: null,
    tags: [],
    progressLog: [],
    createdAt: 1,
    updatedAt: 2,
    messageCount: 1,
    mode: 'primary',
    ...overrides,
  }
}

assert.equal(
  composerExecModeForSession(makeSession({
    execMode: 'task',
    companyProfile: 'corporate',
    isCompanyRuntime: true,
    workItemProjectionId: 'stale-company-marker',
  }), 'company'),
  'task',
)

assert.equal(
  composerExecModeForSession(makeSession({
    isCompanyRuntime: true,
    workItemProjectionId: 'legacy-company-marker',
  }), 'task'),
  'company',
)

assert.equal(
  composerExecModeForSession(makeSession({
    execMode: 'org',
    companyProfile: 'custom',
    orgId: 'quantum_harbor',
  }), 'task'),
  'org',
)

const independentlyPaged = makeSession({
  hasMore: false,
  summaryHasMore: false,
  fullHasMore: true,
  messageCount: 400,
})
assert.equal(sessionHasMoreForDetail(independentlyPaged, 'summary'), false)
assert.equal(sessionHasMoreForDetail(independentlyPaged, 'full'), true)
assert.equal(
  conversationHasOlderHistory([independentlyPaged], 200, 'summary'),
  false,
  'a full-detail ACK must not reopen summary history',
)
assert.equal(
  conversationHasOlderHistory([independentlyPaged], 200, 'full'),
  true,
  'full history must retain its own cursor state',
)
assert.equal(
  conversationHasOlderHistory([independentlyPaged], 200, 'full', false),
  true,
  'a known scoped cursor remains loadable while a company turn is running',
)

const summaryOnlyState = makeSession({
  hasMore: false,
  summaryHasMore: true,
  messageCount: 400,
})
assert.equal(
  sessionHasMoreForDetail(summaryOnlyState, 'full'),
  undefined,
  'generic hasMore is no longer authoritative after a scoped policy is known',
)
assert.equal(
  conversationHasOlderHistory([makeSession({ messageCount: 400 })], 200, 'summary', false),
  false,
  'only the racy message-count fallback is suppressed during active generation',
)

console.log('ContextPanel composer identity checks passed')
