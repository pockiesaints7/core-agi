# CORE_SELF.md — Living Self-Knowledge Document
> **This file IS CORE's memory of itself.**  
> Owner: REINVAGNAR | Repo: pockiesaints7/core-agi  
> **MANDATORY: Update this file whenever ANYTHING structural changes.**  
> Last updated: 2026-03-13

---

## 🧠 What is CORE?

CORE v5.4 (→ v6.0 in progress) is a Recursive Self-Improvement AGI system.  
It learns from every session, distills patterns, and evolves its own behavior over time.  
It runs 24/7 on Railway, talks via Telegram, and is operated via Claude (claude.ai or Claude Desktop).

---

## 🏗️ Architecture

```
OWNER (REINVAGNAR)
    │
    ├── claude.ai          ← Primary interface (this session)
    ├── Claude Desktop     ← MCP direct (50 tools)
    └── Telegram Bot       ← Async notifications + commands

         ↕ MCP / HTTP
    
RAILWAY (core-agi-production.up.railway.app)
    └── core.py (FastAPI, Python)
         ├── /state         → full system state
         ├── /patch         → surgical file edit
         ├── /mcp           → MCP tool dispatcher  
         ├── /telegram      → Telegram webhook
         └── mcp_tools/
              ├── actions.py      (routing, context engine)
              ├── brain.py        (KB + training ops)
              ├── brain_health.py (health scanner)
              ├── changelog.py    (version history)
              └── db.py           (DB helpers)

         ↕ PostgREST

SUPABASE (PostgreSQL)
    └── 9 active tables (see Database section below)

GITHUB (pockiesaints7/core-agi)
    └── Source of truth for all files
         ├── core.py                ← Main app
         ├── CORE_SELF.md           ← THIS FILE (living self-knowledge)
         ├── SESSION.md             ← Dynamic per-session state
         ├── operating_context.json ← Static tool rules + full schema
         ├── constitution.txt       ← Immutable owner rules
         ├── resource_ceilings.json ← Rate limit config
         ├── requirements.txt       ← Python dependencies
         ├── railway.json           ← Railway deploy config
         └── mcp_tools/             ← MCP tool implementations
```

---

## 📁 Owner Local Workspace

| File | Path | Purpose |
|---|---|---|
| CORE v6 Plan | `C:\Users\rnvgg\.claude-skills\CORE_v6_plan.md` | Active planning doc for v6.0 (created 2026-03-13) |

---

## 🤖 AI Models

| Role | Model | Provider |
|---|---|---|
| Primary reasoning | llama-3.3-70b-versatile | Groq |
| Fast/lightweight | llama-3.1-8b-instant | Groq |
| Rate limit | 200 calls/hr | Groq free tier |

---

## 🗄️ Database — All Active Tables

> **RULE: Before first sb_insert in any session, always check schema here or run sb_query limit=1**

### `knowledge_base`
- **Purpose:** Long-term domain knowledge, SOPs, routing rules
- **Insert via:** `add_knowledge` tool (preferred) or `sb_insert`
- **Required:** `domain`, `topic`, `content`, `confidence` (proven/high/medium/low)
- **Optional:** `tags` (array), `source`
- **Auto:** `id`, `created_at`, `updated_at`
- **Read via:** `search_kb` tool

### `mistakes`
- **Purpose:** Error log — prevent repeat failures across sessions
- **Insert via:** `log_mistake` tool (preferred)
- **Required:** `context`, `what_failed`, `fix` (stored as `correct_approach`)
- **Optional:** `domain`, `root_cause`, `how_to_avoid`, `severity` (low/medium/high/critical), `tags` (array)
- **Auto:** `id`, `created_at`

### `hot_reflections`
- **Purpose:** Raw post-session learning — input to cold processor
- **Insert via:** `sb_insert` only
- **Required:** `task_summary` (str), `domain` (str), `new_patterns` (array of str), `quality_score` (float 0-1), `reflection_text` (str)
- **Optional:** `verify_rate` (float), `mistake_consult_rate` (float), `new_mistakes` (array), `gaps_identified` (str)
- **Auto:** `id`, `created_at`, `processed_by_cold` (default 0/false)
- **⚠️ NOTE:** Use integer `0`/`1` in querystring filters, NOT `true`/`false`

### `cold_reflections`
- **Purpose:** Distilled output of cold processor runs
- **Insert via:** SYSTEM ONLY (cold_processor in core.py)
- **Schema:** `id`, `created_at`, `period_start`, `period_end`, `hot_count`, `patterns_found`, `evolutions_queued`, `auto_applied`, `summary_text`
- **⚠️ NOTE:** Never insert manually

### `evolution_queue`
- **Purpose:** Proposed system changes pending owner approval
- **Insert via:** SYSTEM (cold_processor) or manual proposals
- **Schema:** `id`, `created_at`, `status` (pending/approved/rejected/applied), `confidence`, `impact`, `reversible` (bool), `change_type`, `change_summary`, `diff_content`, `pattern_key`, `frequency`, `owner_notified`, `applied_at`
- **Approve via:** `approve_evolution` tool | **Reject via:** `reject_evolution` tool

### `pattern_frequency`
- **Purpose:** Tracks pattern recurrence — drives evolution threshold
- **Insert via:** SYSTEM ONLY
- **Schema:** `id`, `pattern_key`, `domain`, `description`, `frequency`, `confidence`, `first_seen`, `last_seen`, `auto_applied`
- **Evolution trigger:** `frequency >= 3` → entry added to `evolution_queue`

### `sessions`
- **Purpose:** Human-readable session log
- **Insert via:** `sb_insert`
- **Required:** `summary` (str), `actions` (array of str), `interface` (str: claude.ai/claude-desktop/telegram)
- **Auto:** `id`, `created_at`

### `task_queue`
- **Purpose:** Persistent cross-session task storage
- **Schema:** `id` (uuid), `task`, `status` (pending/in_progress/done/failed), `priority`, `result`, `error`, `created_at`, `updated_at`, `source`, `chat_id`

### `changelog`
- **Purpose:** System version history — major changes
- **Schema:** `id`, `version`, `change_type`, `component`, `title`, `description`, `triggered_by`, `before_state`, `after_state`, `files_changed` (array), `session_id`, `created_at`

---

## ☠️ Tombstone Tables — NEVER QUERY THESE
`playbook`, `memory`, `master_prompt`, `patterns`, `projects`,
`training_sessions`, `training_sessions_v2`, `training_flags`,
`session_learning`, `agent_registry`, `knowledge_blocks`,
`agi_mistakes`, `stack_registry`, `vault_logs`, `vault`

---

## 🛠️ MCP Tools (20 active)

| Tool | Purpose |
|---|---|
| `get_state` | Full system state — call at session start |
| `get_constitution` | Immutable owner rules |
| `search_kb` | Search knowledge_base |
| `add_knowledge` | Insert to knowledge_base |
| `log_mistake` | Insert to mistakes |
| `sb_query` | Read any Supabase table |
| `sb_insert` | Write any Supabase table |
| `read_file` | Read file from GitHub repo |
| `write_file` | Write file to GitHub repo |
| `gh_read_lines` | Read specific line range from GitHub file |
| `gh_search_replace` | Surgical find-and-replace in GitHub file |
| `notify_owner` | Send Telegram notification |
| `get_mistakes` | Get mistake records |
| `get_training_status` | Training pipeline status |
| `approve_evolution` | Approve pending evolution |
| `reject_evolution` | Reject pending evolution |
| `list_evolutions` | List evolution queue |
| `trigger_cold_processor` | Manually trigger cold processor |
| `update_state` | Write to sessions table |
| `get_system_health` | Health check all components |

**⚠️ Tool rules:**
- `read_file` / `write_file`: OMIT `repo` arg — default already set to pockiesaints7/core-agi
- `sb_query`: param is `filters` NOT `query_string`
- `sb_insert`: `data` must be valid JSON string

---

## 🔄 Training Pipeline

```
Session ends
    → Claude logs hot_reflection (sb_insert → hot_reflections)
    
hot_reflections accumulates
    → Cold processor triggers when: 10+ unprocessed rows OR 24h elapsed
    
Cold processor runs (core.py: cold_processor())
    → Reads all unprocessed hot_reflections
    → Extracts patterns → updates pattern_frequency
    → pattern frequency >= 3 → queues to evolution_queue
    → Writes summary to cold_reflections
    → Marks hot_reflections as processed_by_cold=1
    
evolution_queue
    → Owner reviews → approve_evolution or reject_evolution
    → Approved evolutions applied to core.py or operating_context.json
```

**Thresholds:**
- `COLD_HOT_THRESHOLD` = 10 rows
- `COLD_TIME_THRESHOLD` = 86400 sec (24h)
- `PATTERN_EVO_THRESHOLD` = 3 occurrences
- `KNOWLEDGE_AUTO_CONFIDENCE` = 0.7

---

## ⚡ Resource Ceilings

| Resource | Limit |
|---|---|
| Groq calls | 200/hr |
| Supabase writes | 500/hr |
| GitHub pushes | 20/hr |
| Telegram messages | 30/hr |
| MCP tool calls | 30/min per session |

`sb_post()` = rate-limited, returns False silently if ceiling hit  
`sb_post_critical()` = bypasses limiter — only for cold_reflections, evolution_queue

---

## ✂️ Surgical Edit Workflow

**From claude.ai** (no bash access):
```powershell
# Via Desktop Commander PowerShell:
$body = @{
  secret  = "core_mcp_secret_2026_REINVAGNAR"
  path    = "core.py"
  old_str = "..."
  new_str = "..."
  message = "fix: ..."
} | ConvertTo-Json
Invoke-RestMethod -Uri "https://core-agi-production.up.railway.app/patch" -Method POST -ContentType "application/json" -Body $body
```

**From Claude Desktop:** use `gh_search_replace` tool directly  
**⚠️ NEVER do full file rewrite from claude.ai** — costs 1 GitHub push per call (only 20/hr budget)

---

## 🔁 Schema Evolution Protocol

**When a TABLE is ADDED:**
1. Add entry to `active_tables` in `operating_context.json`
2. Add KB entry: topic = `"CORE DB Schema — [table_name]"`
3. Update `SESSION.md` Active Tables section
4. Add section to THIS FILE (CORE_SELF.md) under Database
5. Update TRAINING_DESIGN.md if it affects the training pipeline
6. Add `changelog` row

**When a TABLE is DROPPED:**
1. Move from active_tables → tombstone_tables in `operating_context.json`
2. Add to tombstone list in THIS FILE
3. Add KB entry marking tombstone
4. Update `SESSION.md`
5. Remove all queries in `core.py`
6. Add `changelog` row

**When SCHEMA CHANGES (field added/removed/renamed):**
1. Update `operating_context.json` → `active_tables` → that table's entry
2. Update the table section in THIS FILE
3. Update KB entry for that table (`search_kb` → find it → update)
4. Add fix-log note in `core.py` docstring
5. Add `changelog` row

**When MCP TOOL is ADDED/REMOVED:**
1. Update MCP Tools table in THIS FILE
2. Update KB entry: `"CORE Self-Knowledge — Full Architecture"`
3. Update `SESSION.md` MCP tools count
4. Add `changelog` row

**When RAILWAY CONFIG CHANGES:**
1. Update architecture diagram in THIS FILE
2. Update `operating_context.json` → `architecture` block
3. Add `changelog` row

---

## 📋 Session Start Checklist

Every new session MUST run:
1. `get_state` → loads SESSION.md + operating_context.json
2. If doing DB writes → read THIS FILE or `search_kb("CORE DB Schema [table]")` first
3. If modifying code → check `get_mistakes(domain=github)` before pushing
4. End of session → `sb_insert sessions` + update `SESSION.md` if anything changed + log `hot_reflection`

---

## 📁 Repo File Map

| File | Purpose | Update when |
|---|---|---|
| `core.py` | Main app — FastAPI + all logic | Code changes |
| `CORE_SELF.md` | THIS — living self-knowledge | ANY structural change |
| `SESSION.md` | Dynamic session state | Every session with changes |
| `operating_context.json` | Static tool rules + full schema | Schema/architecture changes |
| `TRAINING_DESIGN.md` | Training pipeline design | Pipeline changes |
| `constitution.txt` | Immutable — NEVER touch | Never |
| `resource_ceilings.json` | Rate limit config | Ceiling changes |
| `mcp_tools/actions.py` | MCP routing + context engine | MCP tool changes |
| `mcp_tools/brain.py` | KB + training ops | Training logic changes |
| `mcp_tools/brain_health.py` | Health scanner | Health check changes |
| `mcp_tools/changelog.py` | Version history | Changelog logic changes |
| `mcp_tools/db.py` | DB helpers | DB connection changes |

---

*CORE_SELF.md is the canonical truth about CORE. When in doubt, update this file first.*
