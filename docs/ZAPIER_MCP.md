# ZAPIER MCP — CORE AGI Usage Guide
> Created: 2026-03-13 | Owner: REINVAGNAR | Status: Living doc

---

## What This Is

Zapier MCP is CORE's external action layer. It connects CORE to 8000+ apps without requiring
individual API keys or custom code. Actions are enabled at the config URL, then immediately callable
as `zapier:<app>_<action>` MCP tools from any Claude Desktop session.

**Config URL:** https://mcp.zapier.com/mcp/servers/1b142a72-dda7-4341-a414-0a55cc6ee99f/config
**Full connection map:** docs/ZAPIER_CONNECTIONS.md

> ⚠️ No t_zapier_trigger tool needed — zapier:* tools are callable directly from Claude Desktop.
> Calling them from core.py is NOT required. They are peer tools, not wrapped tools.

---

## Critical Usage Rules

| Rule | Detail |
|---|---|
| `output_hint` | ALWAYS supply — without it, Zapier returns raw JSON blobs that waste context |
| `instructions` | Plain English, be specific especially for writes and deletes |
| Named params | Supply directly for critical operations — don't rely on instructions inference alone |
| Rate limits | Don't loop Zapier calls in bulk. One call per action. |
| No triggers | Zapier MCP = actions only. App→CORE flows still need a real Zap with trigger. |

### output_hint examples
```
"just the email ID and subject"
"confirm success only, no extra data"
"return task ID and due date only"
"return file ID and URL only"
```

---

## Currently Enabled Tools (as of 2026-03-13)

| Tool | Domain | Status |
|---|---|---|
| `zapier:google_drive_retrieve_files_from_google_drive` | Storage | ✅ Enabled |
| `zapier:google_drive_delete_file` | Storage | ✅ Enabled |
| `zapier:google_drive_delete_file_permanent` | Storage | ✅ Enabled |
| `zapier:google_sheets_delete_spreadsheet_row_s` | Sheets | ✅ Enabled |
| `zapier:google_sheets_delete_sheet` | Sheets | ✅ Enabled |
| `zapier:google_sheets_api_request_beta` | Sheets | ✅ Enabled |
| `zapier:google_docs_api_request_beta` | Docs | ✅ Enabled |
| `zapier:google_drive_api_request_beta` | Drive | ✅ Enabled |
| `zapier:google_slides_api_request_beta` | Slides | ✅ Enabled |
| `zapier:github_submit_review` | GitHub | ✅ Enabled |
| `zapier:get_configuration_url` | Meta | ✅ Enabled |
| `zapier:gmail_send_email` | Email | ✅ Enabled (connected 2026-03) |
| `zapier:gmail_find_email` | Email | ✅ Enabled |
| `zapier:gmail_create_draft` | Email | ✅ Enabled |
| `zapier:gmail_reply_to_email` | Email | ✅ Enabled |
| `zapier:google_calendar_create_detailed_event` | Calendar | ✅ Enabled |
| `zapier:google_calendar_find_events` | Calendar | ✅ Enabled |
| `zapier:google_calendar_update_event` | Calendar | ✅ Enabled |
| `zapier:google_calendar_delete_event` | Calendar | ✅ Enabled |
| `zapier:todoist_create_task` | Tasks | ✅ Enabled |
| `zapier:todoist_update_task` | Tasks | ✅ Enabled |
| `zapier:todoist_mark_task_as_completed` | Tasks | ✅ Enabled |
| `zapier:todoist_find_task` | Tasks | ✅ Enabled |
| `zapier:webhooks_by_zapier_post` | Webhooks | ✅ Enabled |
| `zapier:webhooks_by_zapier_get` | Webhooks | ✅ Enabled |

> **Note:** Items marked ✅ Enabled after 2026-03 need Ki to verify at config URL.
> This doc reflects the target enabled state from ZAPIER_CONNECTIONS.md P0 tier.

---

## P0 Connections — Enable These First

These are the minimum viable Zapier connections for CORE autonomy.
Each requires Ki to connect the app at the config URL.

### 1. Gmail ✅ (should be connected via claude.ai)
```python
# Send session summary to Ki
zapier:gmail_send_email(
    instructions="Send email to ki@example.com with subject 'CORE Session Summary' and body {summary}",
    output_hint="confirm sent, return message ID only"
)
```

### 2. Google Calendar
```python
# Schedule next maintenance window
zapier:google_calendar_create_detailed_event(
    instructions="Create event 'CORE Maintenance' on {date} at {time} in CORE AGI calendar",
    output_hint="return event ID and calendar link only"
)
```

### 3. Todoist
```python
# Queue a task for Ki's manual action
zapier:todoist_create_task(
    instructions="Create task 'Review CORE evolution #{id}' in Inbox with due date today",
    output_hint="return task ID only"
)
```

### 4. Webhooks by Zapier
```python
# Emergency fallback — call any URL
zapier:webhooks_by_zapier_post(
    instructions="POST to https://core-agi-production.up.railway.app/health with empty body",
    output_hint="return HTTP status code only"
)
```

### 5. Perplexity (requires paid API key)
```python
# Real-time web research
zapier:perplexity_ask_question(
    instructions="What are the latest Railway.app deployment issues reported in the last 24 hours?",
    output_hint="2-3 sentence summary only"
)
```

---

## CORE Use Cases

### Alert Ki via email when Railway goes down
```
Trigger: CORE detects Railway health check failure
Action: zapier:gmail_send_email — "CORE Railway is down — {timestamp}"
```

### Queue manual review tasks for Ki
```
Trigger: evolution_queue item needs owner approval
Action: zapier:todoist_create_task — "Review CORE evolution: {summary}"
```

### Log CORE costs to Google Sheets
```
Trigger: Monthly billing cycle or new service added
Action: zapier:google_sheets_create_spreadsheet_row — Date, Service, Cost, Notes
```

### Self-monitor via webhook ping
```
Trigger: background_researcher health check loop
Action: zapier:webhooks_by_zapier_post to /health — verify 200 response
```

---

## Adding New Actions

1. Go to config URL
2. Search for app → select actions → save
3. Immediately available as `zapier:<app>_<action>` in Claude Desktop
4. Add entry to the "Currently Enabled Tools" table above
5. Add KB entry: `add_knowledge(domain='zapier', topic='new tool: zapier:...')`

**Rule: Enable only what CORE actively uses. 5-15 focused actions per domain.**
Too many enabled = Claude picks wrong one when instructions are ambiguous.

---

## What's NOT Possible via Zapier MCP

- Triggers (Zapier → CORE). These require a real Zap with a trigger step.
- Polling (watch for new emails, etc.). Must be Zap-driven.
- Bulk operations in loops — rate limits apply.
- Real-time streaming data.

For triggers/webhooks INTO CORE: use Railway `/telegram` or `/mcp` endpoints as Zap webhook targets.
