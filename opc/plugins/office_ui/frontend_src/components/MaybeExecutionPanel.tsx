import { ExecutionPanel } from '../kanban/ExecutionPanel'
import { getExecutionTurnId } from '../lib/workItemRuntimeIds'
import { roleSummaryFromLegacySession } from '../lib/appUtils'
import type { AgentInfo } from '../types/visual'
import type { Session } from '../types/kanban'

/** Thin wrapper so the execution-panel lookup is a normal component, not a JSX IIFE. */
export function MaybeExecutionPanel({ taskId, sessions, agents, onClose }: {
  taskId: string | null
  sessions: Session[]
  agents: AgentInfo[]
  onClose: () => void
}) {
  if (!taskId) return null

  for (const session of sessions) {
    const payload = session.roleWorkItems
    if (!payload) continue
    for (const role of Object.values(payload)) {
      const row = role.workItems.find(workItem => workItem.executionTurnId === taskId)
      if (!row) continue
      return (
        <ExecutionPanel
          role={role}
          focusedWorkItemId={row.workItemId}
          focusedExecutionTurnId={row.executionTurnId}
          agents={agents}
          onClose={onClose}
        />
      )
    }
  }

  const focused = sessions.find(x => x.taskId === taskId || getExecutionTurnId(x) === taskId)
  if (!focused) return null
  const role = roleSummaryFromLegacySession(focused)
  return (
    <ExecutionPanel
      role={role}
      focusedExecutionTurnId={taskId}
      agents={agents}
      onClose={onClose}
    />
  )
}
