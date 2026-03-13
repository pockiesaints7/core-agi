# CORE AGI — Zapier MCP Skill Graph
> Last updated: 2026-03-14 | Source: live tool enumeration from claude.ai session
> Rule: always supply `output_hint`. Always use `instructions` in plain English.
> Rule: Zapier tools are ACTIONS only — no triggers. App→CORE flows need a real Zap with a trigger.

---

## 📧 GMAIL (12 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `gmail_find_email` | Search inbox by Gmail query syntax | Monitor Railway alerts, find security emails |
| `gmail_send_email` | Send new email (to/cc/bcc/attach) | Notify Ki of CORE events, session summaries |
| `gmail_create_draft` | Create draft without sending | Prepare reports for Ki to review+send |
| `gmail_reply_to_email` | Reply to existing thread (by thread_id) | Respond to incoming requests |
| `gmail_create_draft_reply` | Draft a reply without sending | Queue replies for Ki approval |
| `gmail_archive_email` | Archive a message (by message_id) | Clean up processed alerts |
| `gmail_delete_email` | Move to trash (by message_id) | Delete junk/processed notifications |
| `gmail_add_label_to_email` | Add label to message | Tag Railway alerts, organize by domain |
| `gmail_remove_label_from_email` | Remove label from message | Untag processed emails |
| `gmail_remove_label_from_conversation` | Remove label from entire thread | Bulk untag conversations |
| `gmail_create_label` | Create a new Gmail label | Create "CORE Alerts", "CORE Sessions" labels |
| `gmail_get_attachment_by_filename` | Get attachment by filename + message_id | Extract attached files for processing |

**Key skills unlocked:**
- `CORE → Ki email alert` when Railway goes down or evolution needs review
- `Inbox monitoring` for Railway crash emails, GitGuardian alerts
- `Email triage` — find, label, archive, delete in sequence

---

## 📅 GOOGLE CALENDAR (10 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_calendar_find_events` | Find events by date range / search term (up to 25) | Check Ki's schedule before scheduling maintenance |
| `google_calendar_create_detailed_event` | Create event with full fields (title, time, location, recurrence, reminders, attendees, conferencing) | Schedule CORE maintenance windows, deployment slots |
| `google_calendar_update_event` | Update existing event fields | Reschedule maintenance if conflict found |
| `google_calendar_delete_event` | Delete event by event_id | Cancel scheduled maintenance |
| `google_calendar_quick_add_event` | Create event from natural language text | Fast scheduling ("CORE deploy Friday 3pm") |
| `google_calendar_add_attendee_s_to_event` | Add attendees to existing event | Invite collaborators to maintenance windows |
| `google_calendar_move_event_to_another_calendar` | Move event between calendars | Reorganize CORE calendar vs personal |
| `google_calendar_retrieve_event_by_id` | Get specific event by ID | Verify event details before updating |
| `google_calendar_find_calendars` | List all accessible calendars (up to 250) | Discover available calendars |
| `google_calendar_find_busy_periods_in_calendar` | Find busy time slots in a timeframe | Avoid scheduling during Ki's busy periods |

**Key skills unlocked:**
- `Maintenance window scheduling` — check busy periods → create event in free slot
- `Automated reminders` for evolution review, cold processor runs
- `Schedule awareness` — CORE can check Ki's calendar before doing disruptive actions

---

## ✅ TODOIST (7 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `todoist_create_task` | Create task (title, project, due date, priority, labels, parent, assignee, note) | Queue "Review evolution #N" for Ki |
| `todoist_update_task` | Update existing task (title, due, priority, labels, assignee) | Reschedule or reprioritize tasks |
| `todoist_find_task` | Find task by name in a project | Check if a task already exists before creating |
| `todoist_mark_task_as_completed` | Mark task done by task_id | Close tasks CORE resolves autonomously |
| `todoist_move_task_to_section` | Move task to a section within project | Organize tasks by phase/domain |
| `todoist_add_comment_to_task` | Add comment to task ⚠️ Premium only | Log progress notes on a task |
| `todoist_add_comment_to_project` | Add comment to project ⚠️ Premium only | Add project-level notes |

**Key skills unlocked:**
- `Evolution review queue` — when evolution needs Ki's approval, create Todoist task with due=today
- `Task lifecycle management` — create → comment progress → mark done
- `CORE task mirror` — sync SESSION.md task status to Todoist for Ki's mobile visibility

---

## 🌐 WEBHOOKS (3 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `webhooks_by_zapier_post` | Fire POST request (form or JSON) to any URL | Trigger Railway redeploy, call external APIs, notify Slack |
| `webhooks_by_zapier_get` | Fire GET request with optional query params | Poll external endpoints, fetch status pages |
| `webhooks_by_zapier_custom_request` | Full custom HTTP request (any method, headers, auth) | Complex API calls — PUT, PATCH, DELETE, custom auth |

**Key skills unlocked:**
- `External API calls` without custom integration — call any REST API
- `Emergency fallback` — POST to `/patch` via webhook if Railway MCP is down
- `Slack/Discord notifications` via incoming webhooks
- `Trigger external automations` — call n8n, Make.com, or any webhook URL

---

## 📁 GOOGLE DRIVE (5 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_drive_retrieve_files_from_google_drive` | List/search files by custom query | Find CORE-related docs, reports |
| `google_drive_delete_file` | Move file to trash | Delete old session exports |
| `google_drive_delete_file_permanent` | Permanently delete file (irreversible ⚠️) | Hard delete sensitive files |
| `google_drive_export_file` | Export Google Workspace file to PDF/Word/Excel | Export CORE reports for archiving |
| `google_drive_update_file_folder_name` | Rename file or folder | Rename exported reports with timestamps |

---

## 📄 GOOGLE DOCS (3 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_docs_find_and_replace_text` | Find and replace text in a Doc | Update version strings in living docs |
| `google_docs_upload_document` | Upload/convert a file to Google Docs | Import external docs into Drive |
| `google_docs_api_request_beta` | Raw Google Docs API call | Advanced operations not covered by other tools |

---

## 📊 GOOGLE SHEETS (3 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_sheets_get_spreadsheet_by_id` | Get full spreadsheet data by ID | Read metrics spreadsheet |
| `google_sheets_get_data_range` | Read a specific cell range (A1 notation) | Read specific columns from a sheet |
| `google_sheets_api_request_beta` | Raw Sheets API call | Write rows, batch update, advanced ops |

---

## 🖼️ GOOGLE SLIDES (2 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_slides_refresh_charts` | Refresh Sheets-linked charts in a presentation | Update live dashboards |
| `google_slides_api_request_beta` | Raw Slides API call | Advanced presentation manipulation |

---

## 🐙 GITHUB via ZAPIER (19 tools)
> Note: CORE also has direct `github:*` tools. Prefer `github:*` for core-agi repo operations.
> Use `zapier:github_*` for simpler one-liners or when Zapier's inference helps.

| Tool | What it does | CORE use case |
|---|---|---|
| `github_create_or_update_file` | Create/update a file in a repo | Alternative to direct github: tools |
| `github_create_branch` | Create a new branch | Feature branch for major CORE changes |
| `github_find_branch` | Find branch by name | Verify branch exists before operations |
| `github_delete_branch` | Delete a branch | Cleanup stale branches |
| `github_create_gist` | Create a public/private Gist | Share CORE output snippets publicly |
| `github_submit_review` | Submit PR review (approve/request changes/comment) | Review PRs in core-agi repo |
| `github_add_labels_to_issue` | Add labels to an issue | Categorize issues by domain |
| `github_find_repository` | Find a repo by owner/name | Verify repo exists |
| `github_check_organization_membership` | Check if user is in an org | Verify collaborator status |
| `github_set_profile_status` | Set GitHub profile status | Show CORE is active/deploying |
| `github_create_issue` | Create new issue | Log bugs found by CORE autonomously |
| `github_find_issue` | Find existing issue | Check if bug already reported |
| `github_update_issue` | Update issue (title, labels, state, assignee) | Close issues when CORE fixes them |
| `github_find_pull_request` | Find a PR | Check PR status |
| `github_update_pull_request` | Update PR (title, body, state) | Update PR description |
| `github_create_pull_request` | Create new PR | Open PR for CORE changes |
| `github_create_comment` | Comment on issue or PR | Add context to issues/PRs |
| `github_find_user` | Find a GitHub user | Lookup collaborators |
| `github_find_organization` | Find a GitHub org | Verify org details |

---

## 🧠 COMPOUND SKILLS (multi-tool sequences)

| Skill Name | Tools Used | Description |
|---|---|---|
| **Railway Down Alert** | `gmail_find_email` → `gmail_send_email` | Detect Railway crash email → notify Ki with context |
| **Evolution Review Queue** | `todoist_find_task` → `todoist_create_task` | Check if task exists → create "Review evolution #N" if not |
| **Maintenance Window Booking** | `google_calendar_find_busy_periods_in_calendar` → `google_calendar_create_detailed_event` | Find free slot → book CORE maintenance window |
| **Session Summary to Ki** | `gmail_send_email` | Send formatted session summary after `session_end` |
| **CORE Status Broadcast** | `github_set_profile_status` + `gmail_send_email` | Set GitHub status + email Ki during major deploys |
| **External API Polling** | `webhooks_by_zapier_get` | Poll any external API for status/data |
| **Emergency Patch** | `webhooks_by_zapier_post` | POST to `/patch` if Railway MCP is down |
| **Task Lifecycle** | `todoist_create_task` → `todoist_add_comment_to_task` → `todoist_mark_task_as_completed` | Full task lifecycle for Ki-facing work |
| **Inbox Triage** | `gmail_find_email` → `gmail_add_label_to_email` → `gmail_archive_email` | Find → label → archive processed CORE emails |
| **Bug Auto-Report** | `github_find_issue` → `github_create_issue` | Check if bug exists → create if not |
| **Drive Report Export** | `google_drive_export_file` → `google_drive_update_file_folder_name` | Export report to PDF → rename with timestamp |

---

## ⚠️ Limits & Rules

| Rule | Detail |
|---|---|
| `output_hint` | ALWAYS provide — without it Zapier returns raw JSON blobs that waste context |
| Actions only | No trigger capability — Zapier MCP fires actions, cannot listen |
| Rate limit | Don't loop Zapier calls in bulk — one action per call |
| Todoist Premium | `add_comment_to_task` and `add_comment_to_project` require Premium |
| `gmail_delete_email` | Moves to trash only — use `google_drive_delete_file_permanent` for permanent delete |
| Webhooks POST on GET endpoints | 405 is expected — not a tool failure, wrong endpoint type |
| `webhooks_by_zapier_custom_request` | Most flexible but unforgiving — must supply exact method, headers, data |
| Google API beta tools | `*_api_request_beta` tools expose raw API — powerful but require knowing the Google API spec |
| Prefer `github:*` over `zapier:github_*` | For core-agi repo, direct github: tools are more reliable and don't go through Zapier |
| `google_drive_delete_file_permanent` | Irreversible — always confirm before calling |
