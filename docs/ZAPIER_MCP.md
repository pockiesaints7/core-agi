# ZAPIER MCP — CORE AGI External Action Layer

**Purpose:** Expert reference for using Zapier MCP as CORE AGI's bridge to the outside world.
Zapier connects to 8,000+ apps. Through this MCP, CORE can trigger actions in any of them
without writing custom integrations.

**MCP Server URL:** `https://mcp.zapier.com/mcp/servers/1b142a72-dda7-4341-a414-0a55cc6ee99f/mcp`
**Config URL:** `https://mcp.zapier.com/mcp/servers/1b142a72-dda7-4341-a414-0a55cc6ee99f/config`

---

## MENTAL MODEL

Zapier MCP is NOT a general HTTP client. It is a **curated action registry**.
Ki enables specific actions at the config URL → each becomes `zapier:<app>_<action>` MCP tool.
Claude calls it with `instructions` (plain English) + named params. Zapier handles all auth.
No Zaps needed. No code.

Flow: CORE AGI → zapier MCP tool → Zapier action (pre-authed) → 3rd party app

**Key insight:** Bottleneck is NOT capability (8,000 apps available).
Bottleneck is which actions Ki has enabled. Enable more = CORE can do more.

**To add new actions:** config URL → search app → select actions → save → immediately live.
Rule: Enable only what CORE needs. 5-15 focused actions per domain. Too many = wrong tool picked.

---

## CURRENTLY ENABLED TOOLS (2026-03-12)

| Tool | What it does |
|---|---|
| `zapier:google_drive_retrieve_files_from_google_drive` | Search/list Drive files |
| `zapier:google_drive_delete_file` | Move file to trash |
| `zapier:google_drive_delete_file_permanent` | Permanently delete file |
| `zapier:google_sheets_delete_spreadsheet_row_s` | Delete row(s) from sheet |
| `zapier:google_sheets_delete_sheet` | Delete entire worksheet |
| `zapier:google_sheets_api_request_beta` | Raw Google Sheets API call |
| `zapier:google_docs_api_request_beta` | Raw Google Docs API call |
| `zapier:google_drive_api_request_beta` | Raw Google Drive API call |
| `zapier:google_slides_api_request_beta` | Raw Google Slides API call |
| `zapier:github_submit_review` | Submit PR review on GitHub |
| `zapier:get_configuration_url` | Returns config URL for this MCP server |

Currently only Google Workspace + GitHub. Mostly delete/cleanup operations. Expand to unlock CORE's full potential.

---

## USAGE PATTERNS

Every Zapier tool takes `instructions` (required) + named params (optional).

```
# Minimal — Zapier infers from instructions
zapier:google_drive_retrieve_files_from_google_drive(
    instructions="Find all spreadsheets modified in the last 7 days",
    output_hint="file name, id, and last modified date"
)

# Explicit — supply params directly (use for critical writes)
zapier:google_sheets_delete_spreadsheet_row_s(
    instructions="Delete the row where status is done",
    spreadsheet="CORE Task Tracker",
    worksheet="Sheet1",
    rows="3",
    output_hint="confirm deletion success"
)
```

**output_hint (REQUIRED on every call):**
- Without it: Zapier returns large raw JSON blobs — wastes context window
- Bad: output_hint="the result"
- Good: output_hint="just the file ID and name"
- Good: output_hint="confirm success or error message only"
- Good: output_hint="the new row ID that was created"

**instructions field:** Write like telling a human assistant. Be specific.
- Bad: instructions="do the thing"
- Good: instructions="Find the spreadsheet named CORE Evolution Log and return its ID"
- Good: instructions="Send a Slack message to #core-agi channel saying Step 6 Phase 1 complete"

---

## DECISION TREE — Which tool to use?

```
Need to take an action involving a third-party app?
    └── Is it already handled by core-agi MCP tools?
            ├── YES → use core-agi (cheaper, more reliable)
            └── NO  → Is there a Zapier action for it?
                        ├── YES, enabled → call zapier:<app>_<action>
                        ├── YES, not enabled → go to config URL and enable it
                        └── NO → use Webhooks/HTTP action in Zapier as fallback
```

**Use Zapier for:** outward-facing actions, human-readable reports, third-party integrations
**Use core-agi MCP for:** internal state, memory, training pipeline, code

---

## ARCHITECTURE — Where Zapier fits in CORE

```
CORE INTERNAL (Supabase + Railway)     CORE EXTERNAL (via Zapier MCP)
──────────────────────────────────     ─────────────────────────────────
knowledge_base  ←→ search_kb           Google Workspace  ←→ zapier:google_*
evolution_queue ←→ list_evolutions     Notion            ←→ zapier:notion_*
sessions        ←→ sb_insert           Gmail             ←→ zapier:gmail_*
hot_reflections ←→ add_knowledge       Slack             ←→ zapier:slack_*
mistakes        ←→ log_mistake         GitHub            ←→ zapier:github_*
                                       8000+ more        ←→ zapier:*
```

**RULE: Don't duplicate what CORE MCP already does.**
- Supabase = CORE's memory → don't replace with Airtable
- GitHub = CORE's code → don't replace with Drive
- Telegram = CORE's remote control → Slack is additive, not replacement

---

## KNOWN LIMITATIONS

1. Actions must be pre-enabled — tool not found error = go enable it first
2. instructions is not magic — be explicit for critical writes
3. No triggers — only actions. App→CORE flows still need a real Zap with trigger.
4. Rate limits — don't loop over Zapier calls in bulk; batch in Supabase, trigger once
5. Auth is per-account — MCP server URL contains personal server ID, do not share publicly
6. output_hint affects response size — always use specific strings

---

## EXAMPLE PATTERNS

```
# Log session summary to Google Sheets
zapier:google_sheets_create_spreadsheet_row(
    instructions="Add row to CORE Sessions log",
    spreadsheet="CORE AGI Sessions",
    output_hint="confirm row created with row number"
)

# Notify Ki via Gmail
zapier:gmail_send_email(
    instructions="Send email to Ki with subject CORE Evolution Applied, body: [summary]",
    output_hint="confirm sent, include message ID"
)

# Create Notion milestone page
zapier:notion_create_page(
    instructions="Create page in CORE Milestones database titled Step 6 Complete 2026-03-12 with status Done",
    output_hint="return the new page URL"
)

# Escape hatch — raw API when no action exists
zapier:google_sheets_api_request_beta(
    instructions="Append data to CORE log sheet",
    method="POST",
    url="https://sheets.googleapis.com/v4/spreadsheets/{id}/values/A1:append",
    body='{"values": [["2026-03-12", "Step 6", "complete"]]}',
    output_hint="confirm success"
)
```

---

**See also:** docs/ZAPIER_CONNECTIONS.md — full autonomy map, 17 domains, priority connection order
**Last updated:** 2026-03-12
