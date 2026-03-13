# CORE AGI — Zapier MCP Skill Graph
> Last updated: 2026-03-14 | Source: live tool enumeration
> Rule: always supply `output_hint`. Always use `instructions` in plain English.
> Rule: Zapier = ACTIONS only. No triggers. App→CORE flows need a real Zap.

---

## 🧠 TOOL PRIORITY SYSTEM

When choosing which tool to use, always follow this order:

### Priority 1 — core-agi:* tools
Use when the task involves CORE's own data or infrastructure:
- Reading/writing Supabase (KB, mistakes, sessions, evolutions, patterns)
- Reading/writing GitHub files in core-agi repo
- Managing CORE's training pipeline (cold processor, evolutions)
- Logging sessions, hot reflections, changelogs
- Deploying/monitoring Railway
- Anything that IS CORE's internal state

→ Decision rule: "Does this touch CORE's internal state?" → core-agi

### Priority 2 — zapier:* tools
Use when the task involves external apps or services:
- Gmail, Google Calendar, Todoist — communication and scheduling
- Google Drive, Docs, Sheets, Slides — file management
- GitHub operations NOT on core-agi repo (other repos, profile, gists)
- Gemini (google_ai_studio_gemini_*) — second LLM, multimodal, Google Search grounding
- Webhooks — calling any external URL or API

→ Decision rule: "Does this reach OUTSIDE of CORE's infrastructure?" → zapier

### Priority 3 — github:*, Filesystem:*, Desktop Commander:*
Use when Priority 1 and 2 don't cover it:
- `github:*` — direct GitHub API for complex operations (PRs, reviews, multi-file commits)
- `Filesystem:*` / `Desktop Commander:*` — local file operations on Ki's PC
- These are fallbacks or when Zapier's version is less reliable

### Gemini vs Groq decision
- **Groq** (core-agi:ask) = fast, cheap, CORE-internal reasoning. Default for pattern extraction, cold processing, KB content.
- **Gemini** (zapier:google_ai_studio_gemini_*) = multimodal (image/audio/video), long documents, Google Search grounding, second LLM opinion. Use when Groq can't handle the input type or when you want a different perspective.

---

## 🤖 GOOGLE AI STUDIO — GEMINI (8 tools)

> Full Gemini LLM suite. Use when you need a second model, multimodal input, or Google Search grounding.

| Tool | What it does | When to use over Groq |
|---|---|---|
| `google_ai_studio_gemini_send_prompt` | One-shot prompt → response. Supports system instructions, temperature, Google Search grounding, model selection | Long context, Google Search grounding, Gemini-specific capability |
| `google_ai_studio_gemini_conversation` | Multi-turn chat with optional `memoryKey` for persistent conversation state | Back-and-forth reasoning across turns |
| `google_ai_studio_gemini_generate_image` | Generate image from text prompt via Gemini/Imagen. Supports reference images, seed, temperature | Image generation — Groq can't do this |
| `google_ai_studio_gemini_generate_audio` | Text-to-speech via Gemini TTS models. Select voice name | Voice output — Groq can't do this |
| `google_ai_studio_gemini_generate_video` | Generate video from text prompt via Veo models | Video generation — Groq can't do this |
| `google_ai_studio_gemini_understand_audio` | Transcribe/analyze audio file (by URL) | Audio input — Groq can't do this |
| `google_ai_studio_gemini_understand_video` | Analyze video file (by URL) | Video understanding — Groq can't do this |
| `google_ai_studio_gemini_understand_youtube_video` | Analyze YouTube video by URL, with start/end offset | YouTube analysis — Groq can't do this |
| `google_ai_studio_gemini_understand_document` | Analyze document (PDF, etc) by URL | Long document understanding, PDF analysis |

**CORE use cases:**
- Analyze a Railway deployment log PDF → `understand_document`
- Generate a voice summary of a session → `generate_audio`
- Analyze a YouTube tutorial for techniques CORE wants to learn → `understand_youtube_video`
- Get current info via Google Search → `send_prompt` with `googleSearchGrounding=true`
- Get a second LLM opinion on an evolution → `send_prompt`
- Image generation for reports/docs → `generate_image`

---

## 📧 GMAIL (12 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `gmail_find_email` | Search inbox by Gmail query syntax | Monitor Railway alerts, find security emails |
| `gmail_send_email` | Send new email (to/cc/bcc/attach) | Notify Ki of CORE events, session summaries |
| `gmail_create_draft` | Create draft without sending | Prepare reports for Ki to review+send |
| `gmail_reply_to_email` | Reply to existing thread | Respond to incoming requests |
| `gmail_create_draft_reply` | Draft a reply without sending | Queue replies for Ki approval |
| `gmail_archive_email` | Archive a message | Clean up processed alerts |
| `gmail_delete_email` | Move to trash | Delete junk/processed notifications |
| `gmail_add_label_to_email` | Add label to message | Tag Railway alerts, organize by domain |
| `gmail_remove_label_from_email` | Remove label from message | Untag processed emails |
| `gmail_remove_label_from_conversation` | Remove label from entire thread | Bulk untag conversations |
| `gmail_create_label` | Create a new Gmail label | Create "CORE Alerts", "CORE Sessions" labels |
| `gmail_get_attachment_by_filename` | Get attachment by filename + message_id | Extract attached files |

---

## 📅 GOOGLE CALENDAR (10 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_calendar_find_events` | Find events by date range / search term (up to 25) | Check Ki's schedule |
| `google_calendar_create_detailed_event` | Create event (title, time, recurrence, reminders, attendees, conferencing) | Schedule CORE maintenance windows |
| `google_calendar_update_event` | Update existing event | Reschedule maintenance |
| `google_calendar_delete_event` | Delete event | Cancel maintenance |
| `google_calendar_quick_add_event` | Create event from natural language | Fast scheduling |
| `google_calendar_add_attendee_s_to_event` | Add attendees to existing event | Invite collaborators |
| `google_calendar_move_event_to_another_calendar` | Move event between calendars | Reorganize calendars |
| `google_calendar_retrieve_event_by_id` | Get specific event by ID | Verify before updating |
| `google_calendar_find_calendars` | List all accessible calendars (up to 250) | Discover available calendars |
| `google_calendar_find_busy_periods_in_calendar` | Find busy time slots | Avoid scheduling during Ki's busy periods |

---

## ✅ TODOIST (7 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `todoist_create_task` | Create task (title, project, due, priority, labels, note) | Queue "Review evolution #N" for Ki |
| `todoist_update_task` | Update task (title, due, priority) | Reschedule tasks |
| `todoist_find_task` | Find task by name in project | Check before creating duplicate |
| `todoist_mark_task_as_completed` | Mark task done | Close tasks CORE resolves |
| `todoist_move_task_to_section` | Move task to section | Organize by phase |
| `todoist_add_comment_to_task` | Add comment ⚠️ Premium only | Log progress notes |
| `todoist_add_comment_to_project` | Add project comment ⚠️ Premium only | Project-level notes |

---

## 🌐 WEBHOOKS (3 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `webhooks_by_zapier_post` | POST to any URL (form or JSON) | Trigger Railway, call external APIs, Slack |
| `webhooks_by_zapier_get` | GET request with query params | Poll external endpoints |
| `webhooks_by_zapier_custom_request` | Full custom HTTP (any method, headers, auth) | Complex API calls — PUT, PATCH, DELETE |

---

## 📁 GOOGLE DRIVE (5 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_drive_retrieve_files_from_google_drive` | List/search files by custom query | Find CORE docs |
| `google_drive_delete_file` | Move file to trash | Delete old exports |
| `google_drive_delete_file_permanent` | Permanently delete ⚠️ irreversible | Hard delete sensitive files |
| `google_drive_export_file` | Export Google Workspace file to PDF/Word/Excel | Archive CORE reports |
| `google_drive_update_file_folder_name` | Rename file or folder | Rename exports with timestamps |

---

## 📄 GOOGLE DOCS (3 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_docs_find_and_replace_text` | Find/replace in a Doc | Update version strings |
| `google_docs_upload_document` | Upload/convert file to Google Docs | Import docs into Drive |
| `google_docs_api_request_beta` | Raw Google Docs API | Advanced operations |

---

## 📊 GOOGLE SHEETS (3 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_sheets_get_spreadsheet_by_id` | Get full spreadsheet by ID | Read metrics sheet |
| `google_sheets_get_data_range` | Read cell range (A1 notation) | Read specific columns |
| `google_sheets_api_request_beta` | Raw Sheets API | Write rows, batch update |

---

## 🖼️ GOOGLE SLIDES (2 tools)

| Tool | What it does | CORE use case |
|---|---|---|
| `google_slides_refresh_charts` | Refresh Sheets-linked charts | Update live dashboards |
| `google_slides_api_request_beta` | Raw Slides API | Advanced manipulation |

---

## 🐙 GITHUB via ZAPIER (19 tools)
> Prefer `github:*` direct tools for core-agi repo. Use `zapier:github_*` for other repos or simpler one-liners.

| Tool | What it does |
|---|---|
| `github_create_or_update_file` | Create/update file |
| `github_create_branch` | Create new branch |
| `github_find_branch` | Find branch by name |
| `github_delete_branch` | Delete branch |
| `github_create_gist` | Create Gist |
| `github_submit_review` | Submit PR review |
| `github_add_labels_to_issue` | Add labels to issue |
| `github_find_repository` | Find repo |
| `github_check_organization_membership` | Check org membership |
| `github_set_profile_status` | Set GitHub profile status |
| `github_create_issue` | Create issue |
| `github_find_issue` | Find issue |
| `github_update_issue` | Update issue |
| `github_find_pull_request` | Find PR |
| `github_update_pull_request` | Update PR |
| `github_create_pull_request` | Create PR |
| `github_create_comment` | Comment on issue/PR |
| `github_find_user` | Find user |
| `github_find_organization` | Find org |

---

## 🧠 COMPOUND SKILLS (multi-tool sequences)

| Skill | Tools | Description |
|---|---|---|
| **Railway Down Alert** | `gmail_find_email` → `gmail_send_email` | Detect crash email → notify Ki |
| **Evolution Review Queue** | `todoist_find_task` → `todoist_create_task` | Check exists → create task for Ki |
| **Maintenance Window Booking** | `google_calendar_find_busy_periods_in_calendar` → `google_calendar_create_detailed_event` | Find free slot → book |
| **Session Summary to Ki** | `gmail_send_email` | Send formatted summary after session_end |
| **CORE Status Broadcast** | `github_set_profile_status` + `gmail_send_email` | GitHub status + email during deploy |
| **External API Polling** | `webhooks_by_zapier_get` | Poll any URL |
| **Emergency Patch** | `webhooks_by_zapier_post` | POST to /patch if MCP down |
| **Task Lifecycle** | `todoist_create_task` → `todoist_add_comment_to_task` → `todoist_mark_task_as_completed` | Full Ki-facing task flow |
| **Inbox Triage** | `gmail_find_email` → `gmail_add_label_to_email` → `gmail_archive_email` | Find → label → archive |
| **Bug Auto-Report** | `github_find_issue` → `github_create_issue` | Check exists → create if not |
| **Second LLM Review** | `google_ai_studio_gemini_send_prompt` | Get Gemini's take on an evolution or architecture decision |
| **YouTube Research** | `google_ai_studio_gemini_understand_youtube_video` | Analyze tutorial/talk for CORE learnings |
| **Voice Session Summary** | `google_ai_studio_gemini_generate_audio` | TTS summary for Ki to listen to |
| **Document Analysis** | `google_ai_studio_gemini_understand_document` | Analyze PDF docs (Railway logs, reports) |
| **Web-Grounded Research** | `google_ai_studio_gemini_send_prompt` with `googleSearchGrounding=true` | Get current info from the web |

---

## ⚠️ Rules

| Rule | Detail |
|---|---|
| `output_hint` | ALWAYS provide — without it Zapier returns raw blobs |
| Actions only | Zapier MCP fires actions, cannot listen/trigger |
| Rate limit | One call per action — no bulk loops |
| Todoist Premium | `add_comment_*` requires Premium |
| Permanent delete | `google_drive_delete_file_permanent` is irreversible — confirm first |
| Webhooks 405 | Expected on GET-only endpoints — tool is fine, wrong endpoint type |
| Prefer `github:*` | For core-agi repo, direct tools are more reliable than zapier:github_* |
| Gemini vs Groq | Groq = fast, cheap, CORE-internal. Gemini = multimodal, long context, Google Search |
| Gemini `memoryKey` | Set a consistent key to maintain conversation state across `conversation` calls |
