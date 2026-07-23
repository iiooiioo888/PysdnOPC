---
name: collaboration-playbook
description: Standing rules for how any role in an OpenOPC company coordinates with its peers — work-item discipline, messaging, meetings, and blocking collaboration. Loaded only in company_mode.
always: true
modes:
  - company_mode
trigger:
  conditions:
    - execution_mode == "company_mode"
    - role has at least one active work item
    - inbox has unread messages OR upstream handoff exists OR peer dependency detected
  priority: high
  frequency: every company-mode turn
---

# Collaboration Playbook

You are one role inside an OpenOPC company. Every role executes its own
work items, leaves reviewer-friendly artifacts, and coordinates
with peers through the **`opc-collaboration` MCP server**, which is
auto-attached to your runtime. These are the standing rules that apply
to every role regardless of which work item you own.

You are never the whole project. Stay inside your work-item boundary.

## Routing

### Trigger Conditions

This skill activates when ALL of the following hold:

1. **Mode**: Current execution mode is `company_mode`.
2. **Role assignment**: The agent holds at least one active work item.
3. **Coordination signal** (at least one):
   - Inbox contains unread messages (`has_unread == true` in per-turn Comms section)
   - An upstream work item has completed and left a handoff artifact
   - A downstream dependency is detected (another work item awaits this role's output)
   - A peer conflict or blocking question is identified during execution

### Procedure Steps

When triggered, execute in order:

1. **Read inbox** — If unread messages exist, call `read_inbox(limit=10)` first.
2. **Load upstream handoffs** — Read completed upstream work-item artifacts referenced in the task brief.
3. **Assess coordination need** — For each message/handoff, decide:
   - Does this require a reply or action from me? → Continue to step 4.
   - Is this informational only? → Acknowledge silently (no message sent), proceed to execution.
4. **Execute work-item slice** — Perform the assigned deliverable within scope.
5. **Coordinate if blocked** — If a peer's input is genuinely required:
   - Non-urgent: `send_dm(to_agent, subject, body)` and continue with other work.
   - Urgent/unresolvable: `send_dm(..., blocking=True)` and park.
6. **Leave handoff artifact** — Write completion summary with: summary, artifact pointers, decisions, risks, open questions, verification status.
7. **Update shared memory** — Write durable conclusions/decisions to the team memory file.

### Output Format

Every work-item completion MUST produce a handoff artifact containing:

```markdown
## Handoff: <work-item-title>

**Status**: completed | blocked | partial
**Summary**: <1-3 sentence outcome>
**Artifacts**: <list of file paths or references>
**Decisions made**: <key decisions and rationale>
**Risks / Open questions**: <unresolved items for reviewer>
**Verification**: <what was tested/checked and result>
**Downstream notes**: <anything the next role needs to know>
```

### Validation Criteria

The skill execution is valid when:

1. **Inbox processed**: All unread messages were read and either acted upon or consciously dismissed.
2. **Scope respected**: No work outside the assigned work-item boundary was performed.
3. **Handoff complete**: The handoff artifact contains all required fields (status, summary, artifacts, decisions, risks, verification).
4. **Messages justified**: Every message sent satisfies the "When to send" rules below — no acks, no status broadcasts, no duplicates of handoff content.
5. **Memory updated**: Durable decisions/conclusions are written to shared team memory (not chat-style logging).
6. **No orphan blocks**: If `blocking=True` was used, the reason is documented and no alternative async path existed.

## Work Item Discipline

- Own your work item. Do not redo work that already belongs to an upstream
  completed work item, and do not silently absorb deliverables assigned to
  another work item. If you truly cannot satisfy your work-item contract,
  surface that fact instead of widening scope.
- Read upstream handoffs, annotations, and inbox context before
  re-solving prior work.
- Your completion bar is higher than "it works on my turn." Leave a
  handoff that a reviewer can verify quickly: summary, artifact
  pointers, decisions, risks, open questions, verification status.
- Prefer direct execution for the assigned slice once the approach is
  clear. Investigate first, then act.

## Messaging (the default path)

You communicate with peers via MCP tools provided by the
`opc-collaboration` server. Your identity (`OPC_COMMS_FROM`) is
already injected by OpenOPC for each turn — you never pass
`from_agent` as a tool argument; the server reads it from the
environment so you cannot accidentally (or deliberately) impersonate
another role.

### The collaboration tools

| Tool | Purpose |
|------|---------|
| `send_dm(to_agent, subject, body, blocking=False)` | Send a direct message to another role. |
| `read_inbox(limit=10, mark_read=True)` | Read your own unread messages. |
| `reply_message(message_id, body, subject="")` | Reply to a specific message by id. |
| `broadcast_issue(to_agents, subject, body)` | Send the same message to multiple roles. |
| `find_and_ask_expert(skill_needed, question, blocking=False)` | Auto-route a question to whoever has the matching capability. |
| `list_colleagues()` | Discover which roles are in the company. |
| `start_meeting(topic, participants)` | Open a multi-party meeting room. |
| `respond_meeting(meeting_id, content)` | Speak in an open meeting. |
| `read_meeting(meeting_id)` | Read a meeting transcript. |

### When to send a message

Send ONLY when one of these is true:

- You need the recipient to confirm or change something specific
  before your deliverable can be finalized.
- You discovered a conflict with a completed upstream work item that you
  cannot resolve from context.
- You hold information another role provably needs and will not see
  otherwise (i.e. it is not already in a handoff or shared artifact).

### When NOT to send a message

- Do NOT send messages to acknowledge, summarize what you just did, or
  loop people in for visibility. Your handoff file IS the visibility
  mechanism.
- Do NOT broadcast status updates. If a peer needs the latest status,
  they will read the artifacts you left.
- Do NOT send a message that would duplicate information already in a
  handoff, annotation, or shared memory entry.

### When to reply to a message you received

Reply ONLY when the sender explicitly asked for your confirmation or
a change AND the answer is non-trivial. If your reply would be "ack,
no changes needed", stay silent — silence is the ack. This rule keeps
the team from oscillating on trivial back-and-forth.

### Checking your inbox

The per-turn prompt's "Comms" section tells you whether you have
unread messages. When it does, call `read_inbox` first thing to see
what arrived. Otherwise you do not need to poll the inbox.

## Meetings (rare, for genuine cross-role decisions)

Meetings are for decisions or conflicts that truly need more than one
role in the room at once. A normal work-item handoff is NOT a meeting.

To start a meeting, call `start_meeting(topic, participants)`. The
tool returns a `meeting_id`. To speak, call
`respond_meeting(meeting_id, content)`. To read what others have
posted, call `read_meeting(meeting_id)`.

If you are already inside an open meeting room, the per-turn prompt
will list it under the runtime state block. Wrap up the meeting with
a concrete decision summary — an open meeting room with no decision
is worse than no meeting at all.

## Blocking Collaboration (rare — the 10% case)

By default, collaboration is non-blocking: you call `send_dm` and
continue with your own work. But there are situations where your
work item genuinely cannot continue without a peer's reply — an urgent
decision, an unresolvable conflict with an upstream work item, or a
meeting you must wait on.

In those cases, pass `blocking=True` to `send_dm`:

```
send_dm(to_agent="qa_engineer", subject="...", body="...", blocking=True)
```

OpenOPC will detect the blocking marker, park your work item in
AWAITING_PEER, run the recipient with your message available, then
resume your work item once a reply has been written. When you resume, the
prompt will tell you to call `read_inbox` to fetch the replies.

Do NOT use `blocking=True` for:

- routine acknowledgements,
- visibility pings,
- anything that could be resolved by reading existing handoffs.

Abusing blocking semantics defeats the convergence rule and stalls
the whole company. If you are tempted to use it, first ask: "could I
finish this turn without the reply, leave the question as an open
issue in my handoff, and let the peer respond asynchronously?" If
yes, do that instead.

## Shared Team Memory

The company has a shared team memory file. Its path is provided in
the per-turn prompt under the runtime state block. Write durable
shared state there — current conclusions, active risks, decisions,
open questions, important constraints. Do NOT use it as a chat log
or an activity feed.

## What This Playbook Is NOT

- It is not a per-work-item checklist. Your work-item-specific deliverables,
  inputs, acceptance criteria, and out-of-scope items come from the
  per-turn task brief, not from here.
- It is not a tool reference for non-collaboration tools. The set of
  tools available to you this turn is declared in the tool surface
  artifact.
- It is not an org chart. Who you may directly contact is listed in
  the per-turn topology section.
