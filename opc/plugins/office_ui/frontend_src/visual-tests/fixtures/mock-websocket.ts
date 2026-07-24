/**
 * Mock WebSocket init script for Playwright visual tests.
 *
 * Injected via page.addInitScript() BEFORE the app loads.
 * Overrides window.WebSocket so the app receives deterministic
 * fixture data without a real backend.
 */

/** Fixture data sent to the app after "connection" */
const FIXTURE_MESSAGES: Array<{ type: string; payload: unknown }> = [
  {
    type: 'snapshot',
    payload: {
      project_id: 'test-project',
      agents: {
        'agent-1': {
          id: 'agent-1',
          name: 'Alice',
          role: 'Engineer',
          status: 'working',
          office_id: 'office-main',
          seat_zone: 'desk-1',
          x: 200,
          y: 300,
          anim_status: 'typing',
        },
        'agent-2': {
          id: 'agent-2',
          name: 'Bob',
          role: 'Designer',
          status: 'idle',
          office_id: 'office-main',
          seat_zone: 'desk-2',
          x: 400,
          y: 200,
          anim_status: 'idle',
        },
        'agent-3': {
          id: 'agent-3',
          name: 'Carol',
          role: 'PM',
          status: 'meeting',
          office_id: 'office-main',
          seat_zone: 'meeting-1',
          x: 600,
          y: 400,
          anim_status: 'talking',
        },
      },
      channels: {},
      skills: { recent: [], total: 0 },
      practice: { count: 0, last: null },
    },
  },
  {
    type: 'kanban_view_data',
    payload: {
      boards: [
        { id: 'board-1', name: 'Sprint Board', project_id: 'test-project' },
      ],
      columns: [
        { id: 'col-todo', board_id: 'board-1', name: 'Todo', sort_order: 0 },
        { id: 'col-doing', board_id: 'board-1', name: 'In Progress', sort_order: 1 },
        { id: 'col-review', board_id: 'board-1', name: 'Review', sort_order: 2 },
        { id: 'col-done', board_id: 'board-1', name: 'Done', sort_order: 3 },
      ],
      tasks: [
        {
          id: 'task-1',
          board_id: 'board-1',
          column_id: 'col-todo',
          title: 'Implement login page',
          description: 'Create the login form with validation',
          sort_order: 0,
          status: 'todo',
          assignee_id: null,
          created_at: 1700000000,
        },
        {
          id: 'task-2',
          board_id: 'board-1',
          column_id: 'col-doing',
          title: 'Design system tokens',
          description: 'Define color and spacing tokens',
          sort_order: 0,
          status: 'in_progress',
          assignee_id: 'agent-2',
          created_at: 1700000100,
        },
        {
          id: 'task-3',
          board_id: 'board-1',
          column_id: 'col-review',
          title: 'API integration tests',
          description: 'Write integration tests for REST endpoints',
          sort_order: 0,
          status: 'review',
          assignee_id: 'agent-1',
          created_at: 1700000200,
        },
        {
          id: 'task-4',
          board_id: 'board-1',
          column_id: 'col-done',
          title: 'Project setup',
          description: 'Initialize repository and CI pipeline',
          sort_order: 0,
          status: 'done',
          assignee_id: 'agent-1',
          created_at: 1700000300,
        },
      ],
      work_item_projections: [],
    },
  },
  {
    type: 'collab_sync_push',
    payload: {
      project_id: 'test-project',
      sessions: [
        {
          session_id: 'session-1',
          task_id: 'task-2',
          title: 'Design system tokens',
          status: 'running',
          created_at: 1700000100,
          exec_mode: 'task',
          company_profile: 'corporate',
          assignee_ids: ['agent-2'],
        },
        {
          session_id: 'session-2',
          task_id: 'task-3',
          title: 'API integration tests',
          status: 'running',
          created_at: 1700000200,
          exec_mode: 'company',
          company_profile: 'corporate',
          assignee_ids: ['agent-1'],
        },
      ],
      channels: [
        {
          channel_id: 'ch-1',
          name: 'general',
          channel_type: 'group',
          participants: ['agent-1', 'agent-2', 'agent-3'],
        },
      ],
      boards: [
        { id: 'board-1', name: 'Sprint Board', project_id: 'test-project' },
      ],
      columns: [
        { id: 'col-todo', board_id: 'board-1', name: 'Todo', sort_order: 0 },
        { id: 'col-doing', board_id: 'board-1', name: 'In Progress', sort_order: 1 },
        { id: 'col-review', board_id: 'board-1', name: 'Review', sort_order: 2 },
        { id: 'col-done', board_id: 'board-1', name: 'Done', sort_order: 3 },
      ],
      tasks: [
        {
          id: 'task-1',
          board_id: 'board-1',
          column_id: 'col-todo',
          title: 'Implement login page',
          sort_order: 0,
          status: 'todo',
        },
        {
          id: 'task-2',
          board_id: 'board-1',
          column_id: 'col-doing',
          title: 'Design system tokens',
          sort_order: 0,
          status: 'in_progress',
          assignee_id: 'agent-2',
        },
        {
          id: 'task-3',
          board_id: 'board-1',
          column_id: 'col-review',
          title: 'API integration tests',
          sort_order: 0,
          status: 'review',
          assignee_id: 'agent-1',
        },
        {
          id: 'task-4',
          board_id: 'board-1',
          column_id: 'col-done',
          title: 'Project setup',
          sort_order: 0,
          status: 'done',
          assignee_id: 'agent-1',
        },
      ],
    },
  },
  {
    type: 'org_info',
    payload: {
      org_id: 'org-1',
      name: 'Test Corp',
      roles: [
        { role_id: 'role-eng', name: 'Engineer', headcount: 2 },
        { role_id: 'role-design', name: 'Designer', headcount: 1 },
        { role_id: 'role-pm', name: 'PM', headcount: 1 },
      ],
      employees: [
        { employee_id: 'agent-1', name: 'Alice', role_id: 'role-eng', status: 'active' },
        { employee_id: 'agent-2', name: 'Bob', role_id: 'role-design', status: 'active' },
        { employee_id: 'agent-3', name: 'Carol', role_id: 'role-pm', status: 'active' },
      ],
    },
  },
]

/**
 * This function is serialized and injected into the page context.
 * It replaces window.WebSocket with a mock that auto-connects
 * and delivers fixture messages.
 */
export function getMockWebSocketScript(): string {
  return `
(function() {
  const FIXTURE_MESSAGES = ${JSON.stringify(FIXTURE_MESSAGES)};

  class MockWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor(url) {
      this.url = url;
      this.readyState = MockWebSocket.CONNECTING;
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      this._sendQueue = [];

      // Simulate async connection
      setTimeout(() => {
        this.readyState = MockWebSocket.OPEN;
        if (this.onopen) this.onopen({ type: 'open' });

        // Deliver fixture messages with small delays for realistic rendering
        FIXTURE_MESSAGES.forEach((msg, i) => {
          setTimeout(() => {
            if (this.readyState === MockWebSocket.OPEN && this.onmessage) {
              this.onmessage({ data: JSON.stringify(msg) });
            }
          }, 50 + i * 30);
        });
      }, 10);
    }

    send(data) {
      this._sendQueue.push(data);
      // Auto-respond to certain requests
      try {
        const parsed = JSON.parse(data);
        if (parsed.type === 'list_projects') {
          setTimeout(() => {
            if (this.readyState === MockWebSocket.OPEN && this.onmessage) {
              this.onmessage({ data: JSON.stringify({
                type: 'ack',
                payload: {
                  request_type: 'list_projects',
                  projects: [{ id: 'test-project', name: 'Test Project', path: '/tmp/test' }],
                  active_project_id: 'test-project',
                }
              })});
            }
          }, 20);
        }
        if (parsed.type === 'session_detail') {
          setTimeout(() => {
            if (this.readyState === MockWebSocket.OPEN && this.onmessage) {
              this.onmessage({ data: JSON.stringify({
                type: 'ack',
                payload: {
                  request_type: 'session_detail',
                  task_id: parsed.task_id || 'task-2',
                  messages: [
                    { id: 'msg-1', role: 'user', content: 'Please design the token system', timestamp: 1700000100 },
                    { id: 'msg-2', role: 'assistant', content: 'I will create color, spacing, and typography tokens.', timestamp: 1700000110 },
                  ],
                  has_more: false,
                }
              })});
            }
          }, 20);
        }
      } catch(e) { /* ignore parse errors */ }
    }

    close() {
      this.readyState = MockWebSocket.CLOSED;
      if (this.onclose) this.onclose({ type: 'close', code: 1000, reason: 'mock close' });
    }

    addEventListener(type, handler) {
      if (type === 'open') this.onopen = handler;
      if (type === 'message') this.onmessage = handler;
      if (type === 'error') this.onerror = handler;
      if (type === 'close') this.onclose = handler;
    }

    removeEventListener() {}
  }

  // Preserve original constants
  MockWebSocket.CONNECTING = 0;
  MockWebSocket.OPEN = 1;
  MockWebSocket.CLOSING = 2;
  MockWebSocket.CLOSED = 3;

  window.WebSocket = MockWebSocket;
})();
`
}
