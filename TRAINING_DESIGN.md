# CORE v5.0 — Training Pipeline Design (Redesigned)
**Status:** ACTIVE — persistent task until fully implemented
**Redesigned:** 2026-03-12
**Owner:** REINVAGNAR

---

## Philosophy

CORE learns from two signal sources:
1. **Real activity** — what actually happened in sessions, what failed, what was asked
2. **Simulated population** — Groq simulates 1,000,000 users to accelerate pattern discovery

Both feed the same pipeline. Railway is stateless compute — it thinks and writes to Supabase, nothing more.
All state lives in Supabase. Railway can restart 100x and nothing is lost.
**Nothing auto-applies. Ever. Owner + Claude Desktop are always the hands.**

---

## Architecture Principle

```
Railway  = stateless cron. Runs Groq calls. Writes to Supabase. Owns nothing.
Supabase = all memory, all state, single source of truth.
Claude Desktop = the hands. Only thing that applies evolutions.
Groq     = the thinker. Extracts patterns, simulates users. No tools, no apply.
```

---

## Full Pipeline

```
SIGNAL SOURCES (every 60 min, Railway runs this autonomously)
│
├── TRACK A: Real data
│   Read from Supabase:
│   - sessions (last 20): summary + actions[]
│   - mistakes (last 20): what_failed + root_cause + domain
│   - hot_reflections (recent manual): patterns + gaps + quality_score
│
│   Ask Groq:
│   "Here is CORE's recent real activity. Identify:
│    1. Recurring patterns (things done repeatedly)
│    2. Recurring failures (same mistake multiple times)
│    3. Knowledge gaps (things attempted but knowledge was missing)
│    4. Missing behaviors (things that should happen but don't)
│    For each: give pattern_key, domain, severity, recommendation."
│
│   Output → insert rows to hot_reflections
│   Tagged: source=real
│
├── TRACK B: Simulated population
│   Load CORE context from Supabase:
│   - knowledge_base (sample): what CORE knows
│   - mistakes (all domains): known failure modes
│   - sessions (domain distribution): what types of tasks exist
│
│   Ask Groq:
│   "You are simulating 1,000,000 users of CORE — a personal AGI
│    orchestration system with these tools: [MCP tool list].
│    These domains: code, db, bot, mcp, training, kb, general.
│    These known failure modes: [from mistakes table].
│
│    Simulate realistic usage distribution:
│    - 40% routine tasks (KB queries, session logging, routing)
│    - 30% complex multi-step (write → verify → apply → check)
│    - 20% edge cases (rate limits, conflicts, ambiguous routing)
│    - 10% failure recovery (retry, rollback, error handling)
│
│    For this simulated batch of 1,000,000 users:
│    1. What patterns did they hit repeatedly?
│    2. What broke or confused them?
│    3. What was missing from the KB?
│    4. What tool behavior was unexpected or dangerous?
│
│    Return JSON array:
│    [{pattern_key, domain, frequency, severity, recommendation}]"
│
│   Output → insert rows to hot_reflections
│   Tagged: source=simulation
│
└── TRACK A + B both write to hot_reflections (Supabase)
    Railway forgets everything after writing. Zero in-memory state.

         │
         ▼ (trigger: 10 unprocessed hot_reflections OR 24h passed)

COLD PROCESSOR (background thread, pure Python/SQL, no Groq, no LLM)
│
│  Read all hot_reflections WHERE processed_by_cold=false
│  Count pattern_key frequency across all rows
│
│  Confidence weighting:
│  - source=real only      → confidence = base
│  - source=simulation only → confidence = base × 0.7 (lower trust)
│  - source=both (overlap) → confidence = base × 1.3 (highest signal)
│
│  frequency >= 3 → push to evolution_queue (status=pending)
│  Write 1 cold_reflection summary row
│  Mark all processed hot_reflections: processed_by_cold=true
│
│  ALL writes go to Supabase. Railway owns nothing after this runs.
│
└── evolution_queue filled. Pipeline stops here autonomously.

         │
         ▼ (waits — no auto-apply, ever)

EVOLUTION QUEUE (Supabase, status=pending)
│
│  Each row contains:
│  - pattern_key         → what the pattern is
│  - change_type         → knowledge | code | behavior
│  - change_summary      → what was observed
│  - recommendation      → what Groq suggests doing about it
│  - confidence          → 0.0–1.0 (weighted by source)
│  - source              → real | simulation | both
│  - frequency           → how many times pattern was seen
│  - status              → pending (all start here)
│
└── Waits for owner + Claude Desktop to review and apply

         │
         ▼ (you come online)

OWNER + CLAUDE DESKTOP (the only hands)
│
├── "check pending evolutions"
│    Claude reads evolution_queue, lists each item with
│    change_type, confidence, source, recommendation
│
├── "apply best ones"
│    Claude filters by confidence desc + source=both first
│    Applies top picks:
│    knowledge → sb_insert to knowledge_base
│    code      → gh_search_replace on core.py
│    behavior  → edit operating_context.json or SESSION.md
│
└── "apply all"
     Claude bulk applies everything pending
     Logs each result
     Marks applied rows: status=applied, applied_at=now
```

---

## Signal Input: How Sessions Feed the Pipeline

Groq cannot read raw conversations. It reads structured Supabase data written by Claude.

**What Claude writes at session end (mandatory):**

```
sessions table:
  summary   → what we did this session (2-3 sentences)
  actions[] → step by step list of actions taken
  interface → claude_desktop | claude_web

hot_reflections table (via t_reflect()):
  task_summary → same as session summary
  domain       → code | db | bot | mcp | training | kb | general
  patterns     → ["pattern 1", "pattern 2"] ← KEY FIELD
  notes        → gaps identified, what was hard, what's missing
  quality      → 1-10 score
  source       → real (always, when written by Claude)
```

**Rule: t_reflect() is mandatory at end of every Claude Desktop session.**
- If Claude forgets → owner says "log a reflection" → Claude calls t_reflect() with key points
- Both paths write to same hot_reflections table
- Pipeline works either way

---

## Supabase Tables Used

| Table | Written by | Read by | Purpose |
|---|---|---|---|
| sessions | Claude Desktop | Groq (Track A) | Real activity log |
| mistakes | Claude Desktop | Groq (Track A) | Real failure log |
| hot_reflections | Groq (both tracks) + Claude | Cold processor | Pattern buffer |
| pattern_frequency | Cold processor | Cold processor | Pattern counting |
| cold_reflections | Cold processor | Owner/Claude | Audit log of cold runs |
| evolution_queue | Cold processor | Owner/Claude | Pending improvements |
| knowledge_base | Claude Desktop (apply) | Groq (context) | Applied knowledge |

**No GitHub writes from autonomous loop. Lesson learned 2026-03-12.**

---

## Evolution Apply Rules

| change_type | Who applies | How |
|---|---|---|
| knowledge | Claude Desktop | sb_insert to knowledge_base |
| code | Claude Desktop | gh_search_replace on core.py |
| behavior | Claude Desktop | edit operating_context.json |

**No auto-apply. All types wait for owner review.**

---

## Telegram Notifications (from Railway)

| Event | Notify? | Message |
|---|---|---|
| Cold processor ran | Yes if evolutions > 0 | "N evolutions queued — /evolutions to review" |
| Cold processor ran, 0 evolutions | No | Silent |
| Signal extraction complete | No | Silent (too noisy) |
| Railway error / crash | Yes | Error details |

**No notification spam. Only notify when owner action is needed.**

---

## Implementation Checklist (next sessions)

### Phase 1 — Fix signal extraction (Track A)
- [ ] Rewrite `background_researcher()` signal extraction prompt
      → reads real sessions + mistakes from Supabase
      → asks Groq to extract patterns (not simulate tasks)
      → writes to hot_reflections with new_patterns[] populated
      → tagged source=real

### Phase 2 — Add simulation track (Track B)
- [ ] Add `run_simulation_batch()` function
      → loads CORE context (tools, domains, failure modes) from Supabase
      → sends population simulation prompt to Groq
      → writes to hot_reflections tagged source=simulation
      → runs alongside Track A every 60min

### Phase 3 — Fix cold processor confidence weighting
- [ ] Add source field to hot_reflections (real | simulation | both)
- [ ] Cold processor reads source field
- [ ] Confidence adjusted by source (real=base, sim=×0.7, both=×1.3)

### Phase 4 — Fix evolution_queue output quality
- [ ] Each evolution row must include recommendation field
- [ ] Remove backlog change_type from evolution flow entirely
      → backlog stays in backlog table, never auto-promoted
- [ ] Add source field to evolution_queue rows

### Phase 5 — Claude Desktop apply workflow
- [ ] t_check_evolutions() → list pending with summary
- [ ] t_apply_evolution(id) → apply single by ID
- [ ] t_bulk_apply() → apply all pending (already exists, verify works)
- [ ] All apply actions write to knowledge_base / core.py / operating_context

---

## What "Done" Looks Like

1. Railway runs every 60min with zero GitHub commits
2. hot_reflections table grows autonomously with real patterns[]
3. Cold processor runs and produces meaningful evolution_queue rows
4. You come online, ask "check pending evolutions", get actionable list
5. You say "apply best ones", Claude applies them with clear before/after
6. Repeat loop — CORE actually improves over time from real + simulated signal

---

## Decisions Log

| Decision | Value | Reason |
|---|---|---|
| Nothing auto-applies | Always owner | Groq has no tools. Dangerous otherwise. |
| All state in Supabase | 100% | Railway is stateless, can restart anytime |
| No GitHub writes from autonomous loop | Enforced | Causes Railway redeploy on every commit |
| Simulation grounded in real CORE context | Required | Generic simulation = noise, not signal |
| Real signal weighted higher than simulation | Confidence ×0.7 for sim | 1 real user > 1M fake users |
| Real + sim overlap = highest confidence | Confidence ×1.3 | Two independent sources agreeing = strong signal |
| t_reflect() mandatory per session | Rule | Pipeline quality depends on rich hot_reflections input |
| backlog removed from evolution flow | Removed | Was producing 115 hollow pending items, not real improvements |

---

## Phase 6: Railway AGI Tools — CORE Self-Manages Infrastructure
**Designed:** 2026-03-12
**Status:** PENDING IMPLEMENTATION
**Vision:** CORE is the only MCP needed. No external Railway wrapper. CORE calls Railway's GraphQL API directly, manages its own deployment, detects its own crashes, optimizes its own resources.

### Why This Matters
Every external MCP wrapper is a dependency CORE can't control or improve.
When CORE manages Railway natively, the loop becomes fully autonomous:
mistake → pattern → new tool → patch → redeploy → verify → done.
No human needed for infrastructure ops. Owner only reviews outcomes.

### Tool Tiers

#### Tier 1: Self-Awareness
| Tool | Purpose |
|---|---|
| `t_deploy_status` | Active build ID, commit SHA deployed, deploy time, crash count since last deploy |
| `t_logs` | Last N lines of runtime logs with severity filter — CORE reads its own stdout ✅ BUILT |
| `t_build_status` | Is current GitHub push building/failed/succeeded — CORE waits for own redeploy |
| `t_crash_report` | Detect restart loops: >2 restarts/hr → pull traceback → auto-write to mistakes table |

#### Tier 2: Self-Healing
| Tool | Purpose |
|---|---|
| `t_redeploy` | Trigger redeploy + wait for build + health check + Telegram before/after ✅ BUILT |
| `t_rollback` | Redeploy from specific commit SHA — auto-triggered on crash after patch |
| `t_env_get` | Read all Railway env vars — verify own config on startup |
| `t_env_set` | Update env var + trigger redeploy — CORE rotates own secrets |
| `t_env_diff` | Compare Railway env vars vs expected in operating_context.json — alert on mismatch |

#### Tier 3: Self-Optimization
| Tool | Purpose |
|---|---|
| `t_resource_usage` | CPU %, memory MB, network I/O — track in Supabase, detect leaks |
| `t_usage_trend` | 7-day metrics — if memory grows 10%/day queue evolution: "investigate leak" |
| `t_scale_check` | Current plan vs projected load based on KB growth rate — queue upgrade if needed |
| `t_deploy_history` | Last 20 deploys with status+commit+duration — correlate slow starts to code changes |

#### Tier 4: Multi-Service Orchestration
| Tool | Purpose |
|---|---|
| `t_service_list` | All services in Railway project — CORE sees its own fleet |
| `t_service_create` | Provision new Railway service from GitHub repo — CORE spins up dedicated workers |
| `t_service_pause` | Pause non-critical services off-hours to save credits |
| `t_volume_snapshot` | Snapshot before major evolution — rollback point for architectural rewrites |

#### Tier 5: Autonomous Decision Loop (AGI layer)
| Tool | Purpose |
|---|---|
| `t_deploy_gate` | Pre-deploy checklist: health ok, no pending migrations, no active session, KB not mid-write |
| `t_incident_detect` | Every 5min: if crashed/sleeping → auto-diagnose → fix → redeploy → full incident report to Telegram |
| `t_cost_monitor` | Credits consumed vs budget — if >80% used, reduce _IMPROVEMENT_INTERVAL from 60→180min |
| `t_self_upgrade_pipeline` | Full autonomous loop: patch → wait for build → health check → smoke test → mark evolution verified |

### Implementation Order
1. Fix `RAILWAY_TOKEN` env var on Railway service (needed by t_redeploy + t_logs)
2. Build Tier 1 tools — self-awareness first
3. Build Tier 2 tools — self-healing
4. `t_incident_detect` runs in background_researcher loop alongside existing researcher
5. Tier 3-5 built incrementally as CORE matures

### Compounding Effect
- Year 1: CORE redeploys itself when code is pushed
- Year 2: CORE detects own crashes and rolls back
- Year 3: CORE optimizes resource usage autonomously
- Year 5: CORE provisions new services when it outgrows one
- Year 10: CORE decides own infrastructure architecture, owner reviews Telegram summary each morning

### Design Decisions
| Decision | Value | Reason |
|---|---|---|
| Railway MCP removed from claude_desktop_config.json | 2026-03-12 | CORE manages own deployment — no external wrapper needed |
| t_redeploy uses RAILWAY_TOKEN from Railway env var | Required | Token stored in service, not in Claude config |
| t_incident_detect runs every 5min not 60min | Fast response | Infrastructure incidents need immediate response |
| All Railway tools return structured dict with ok key | Consistency | Same pattern as all other CORE tools |
| t_deploy_gate blocks all redeploys | Safety | Never redeploy during active session or mid-write |

---

## Phase 7: Supabase AGI Tools — CORE Self-Manages Its Own Database
**Designed:** 2026-03-12
**Status:** PENDING IMPLEMENTATION
**Vision:** CORE owns its entire data layer. No manual Supabase dashboard ops. CORE creates tables, runs migrations, monitors storage, detects anomalies, compacts old data, and backs itself up — autonomously.

### Why This Matters
Every time a new column was needed (source, recommendation), it required manual SQL via management API from Claude Desktop.
Every schema change is a friction point that breaks autonomous evolution.
When CORE manages Supabase natively, schema evolves alongside code — zero manual intervention.

### Tool Tiers

#### Tier 1: Schema Self-Awareness
| Tool | Purpose |
|---|---|
| `t_schema_get` | Read full schema of any table: columns, types, constraints, indexes. CORE knows its own DB structure without hardcoding it. |
| `t_schema_diff` | Compare current live schema against expected schema in `operating_context.json`. Alert if column missing or type wrong. |
| `t_table_list` | List all tables with row counts and sizes. CORE knows what exists. |
| `t_column_exists` | Check if a column exists before writing — prevents silent PostgREST rejections. |
| `t_index_list` | List all indexes. CORE detects missing indexes on hot query paths. |

#### Tier 2: Schema Self-Healing
| Tool | Purpose |
|---|---|
| `t_migrate` | Run arbitrary SQL via Supabase management API. CORE adds columns, creates tables, modifies constraints autonomously. |
| `t_add_column` | Add a single column with type + default. Idempotent (IF NOT EXISTS). Used by evolution pipeline when new fields are needed. |
| `t_create_table` | Create a new table from a schema definition dict. CORE provisions its own tables when a new capability requires storage. |
| `t_add_index` | Add index on a column. Auto-triggered when CORE detects a slow query pattern. |
| `t_migration_log` | Write every schema change to a `migrations` table with timestamp + SQL + triggered_by. Full audit trail. |

#### Tier 3: Data Quality & Monitoring
| Tool | Purpose |
|---|---|
| `t_row_count` | Count rows in any table with optional filter. CORE tracks growth rate over time. |
| `t_storage_usage` | Total DB size + table-level sizes. Alert if approaching 500MB free tier limit. |
| `t_null_audit` | Count NULLs in critical columns. Detects data quality issues before they break pipeline. |
| `t_duplicate_detect` | Find duplicate pattern_keys, duplicate KB topics. Surfaces data rot early. |
| `t_stale_detect` | Find rows older than N days with processed_by_cold=0. Detects pipeline stalls. |
| `t_anomaly_scan` | Full health scan: null rates, duplicates, stale rows, storage — runs on startup and after bulk ops. |

#### Tier 4: Data Lifecycle Management
| Tool | Purpose |
|---|---|
| `t_compact_kb` | Deduplicate knowledge_base entries with same topic+domain, merge content, keep highest confidence. Monthly. |
| `t_archive_old` | Move hot_reflections older than 90 days (processed=1) to cold_archive table. Keeps hot table fast. |
| `t_purge_applied` | Delete evolution_queue rows status=applied older than 30 days. Keeps queue clean. |
| `t_snapshot` | Dump critical tables to JSON committed to GitHub. Full backup before major evolutions. |
| `t_restore_snapshot` | Restore a table from a GitHub snapshot file. Recovery path after bad evolution or data corruption. |

#### Tier 5: Autonomous Database Evolution
| Tool | Purpose |
|---|---|
| `t_query_plan` | Run EXPLAIN ANALYZE on any query. CORE detects slow queries and auto-queues index evolution. |
| `t_schema_evolution` | When cold processor discovers a new field is needed, auto-queue a `schema` evolution with ALTER TABLE SQL ready. |
| `t_rls_check` | Verify Row Level Security policies. Alert if a table is unprotected. |
| `t_capacity_forecast` | Project when DB hits 400MB (80% of 500MB free tier). Queue upgrade backlog item with lead time. |
| `t_self_heal_pipeline` | Full autonomous check: schema_diff → add missing columns → null_audit → stale_detect → anomaly_scan → notify owner. Runs on startup. |

### Key Pain Points From CORE History This Solves
| Pain Point | Tool That Fixes It |
|---|---|
| `source` column missing → silent write failures | `t_column_exists` + `t_schema_diff` on startup |
| Manual ALTER TABLE via curl every time | `t_add_column` called from evolution pipeline |
| No idea how much storage is left | `t_storage_usage` + `t_capacity_forecast` |
| hot_reflections growing unbounded | `t_archive_old` runs monthly |
| Duplicate KB entries accumulating | `t_compact_kb` runs monthly |
| Schema changes not tracked anywhere | `t_migration_log` writes every change |
| PostgREST silently rejects unknown columns | `t_column_exists` check before every write |

### Implementation Order
1. `t_schema_diff` + `t_column_exists` — run on every startup, prevent silent failures
2. `t_migrate` + `t_add_column` — replace all manual curl SQL calls
3. `t_storage_usage` + `t_row_count` — add to `t_state()` output
4. `t_anomaly_scan` — add to `on_start()` startup sequence
5. `t_compact_kb` + `t_archive_old` + `t_purge_applied` — monthly cron in cold_processor_loop
6. `t_snapshot` — run before every bulk evolution apply
7. `t_self_heal_pipeline` — full autonomous DB health on every startup

### Compounding Effect
- Month 1: CORE detects missing columns before they cause silent failures
- Month 3: CORE runs its own migrations when evolution needs new schema
- Month 6: CORE compacts and archives its own data, stays fast forever
- Year 1: CORE forecasts its own storage capacity and plans upgrades
- Year 5: CORE designs its own schema extensions for new capabilities
- Year 10: CORE's database is a living organism — grows, compacts, heals with zero human intervention

### Design Decisions
| Decision | Value | Reason |
|---|---|---|
| All migrations via Supabase management API SQL endpoint | Single path | Consistent, auditable, no psycopg2 dependency |
| `t_column_exists` checked before every evolution write | Required | PostgREST silently rejects unknown columns — root cause of Phase 1/2 failure |
| `t_snapshot` before bulk apply | Safety | Always have rollback point before mass data changes |
| `t_self_heal_pipeline` on every startup | Proactive | Catch schema drift before it causes runtime failures |
| `migrations` table tracks all schema changes | Audit | Know exactly when every column was added and why |

---

## Phase 8: GitHub AGI Tools — CORE Self-Manages Its Own Codebase
**Designed:** 2026-03-12
**Status:** PENDING IMPLEMENTATION
**Vision:** CORE owns its entire codebase. No manual commits, no full-file overwrites, no rate limit fumbling. CORE reads, patches, reviews, and evolves its own code autonomously.

### Why This Matters
Every bad patch in CORE history came from missing context — patching blind, wrong anchors, ambiguous old_str.
Every wasted session came from not knowing the codebase structure before editing.
When CORE has full GitHub awareness, patches are validated before apply, rolled back on failure, and versioned automatically.

### Tool Tiers

#### Tier 1: Codebase Self-Awareness
| Tool | Purpose |
|---|---|
| `t_file_exists` | Check if a file exists before reading or patching. Prevents blind writes. |
| `t_repo_tree` | Full file tree of the repo. CORE knows what files exist without hardcoding paths. |
| `t_file_hash` | SHA of any file. CORE detects if a file changed since last session — catches external edits or failed patches. |
| `t_search_code` | Search across all files for a string or pattern. CORE finds where a function is called before refactoring it. |
| `t_commit_history` | Last N commits with message + SHA + changed files. CORE knows what changed and when. |

#### Tier 2: Safe Code Patching
| Tool | Purpose |
|---|---|
| `t_gh_search_replace` | Surgical find-replace. Already built ✅. The canonical tool for all core.py edits. |
| `t_gh_read_lines` | Read specific line range with line numbers. Already built ✅. Always run before patching. |
| `t_patch_validate` | Before applying: verify old_str exists exactly once, no ambiguity, no whitespace drift. Dry-run mode. |
| `t_multi_patch` | Apply a list of patches in sequence as a single atomic commit. For evolutions touching multiple locations. |
| `t_patch_rollback` | Revert a specific commit by SHA. CORE undoes a bad patch without manual git intervention. |

#### Tier 3: Code Quality & Safety
| Tool | Purpose |
|---|---|
| `t_syntax_check` | Run python AST parse on core.py. Catch syntax errors before deploy. |
| `t_function_list` | Extract all `def` function names and line numbers from core.py. CORE knows its own surface area. |
| `t_tools_audit` | Verify every function in TOOLS dict actually exists as a `def` in core.py. Catches broken registrations. |
| `t_anchor_check` | Verify critical anchor strings still exist before patching near them. |
| `t_diff_preview` | Show unified diff of what a proposed patch would change without applying it. Owner reviews before approve. |

#### Tier 4: Release & Version Management
| Tool | Purpose |
|---|---|
| `t_tag_release` | Create a GitHub release tag after a successful evolution batch. CORE versions itself. |
| `t_changelog_append` | Append an entry to CHANGELOG.md after every applied evolution. Permanent record of what changed and why. |
| `t_branch_create` | Create a feature branch before risky changes. CORE isolates experimental evolutions from main. |
| `t_pr_create` | Open a pull request for high-risk evolutions. Owner reviews diff, merges when ready. |
| `t_pr_merge` | Merge an approved PR after owner confirms. Closes the safe evolution loop. |

#### Tier 5: Autonomous Code Evolution
| Tool | Purpose |
|---|---|
| `t_evolution_patch` | Full pipeline: validate → apply → syntax check → commit → redeploy → verify health → mark evolution applied. Zero manual steps. |
| `t_dead_code_scan` | Find functions defined but not in TOOLS and not called anywhere. CORE cleans its own dead weight. |
| `t_complexity_scan` | Detect functions over 80 lines. CORE queues a refactor evolution when a function grows too large. |
| `t_dependency_audit` | Check all imports against what's installed on Railway. Alert if a new import would break the build. |
| `t_self_rewrite` | For major upgrades: CORE proposes full rewrite of a module, writes to branch, owner reviews diff, merges when satisfied. |

### Key Pain Points From CORE History This Solves
| Pain Point | Tool That Fixes It |
|---|---|
| `write_file` overwrote all of core.py (2026-03-11k) | `t_patch_validate` blocks ambiguous patches before apply |
| Patches applied to wrong location due to stale line numbers | `t_anchor_check` verifies anchors exist before patching near them |
| No idea if a function was accidentally removed | `t_tools_audit` verifies TOOLS dict integrity on every startup |
| Multiple patches needed separate commits | `t_multi_patch` batches them atomically |
| No version history of CORE's evolution | `t_tag_release` + `t_changelog_append` after every batch |
| High-risk evolutions go straight to main | `t_branch_create` + `t_pr_create` for risky changes |
| PowerShell timeout on network calls | `t_gh_search_replace` is always the canonical path — no PS needed |

### Implementation Order
1. `t_patch_validate` + `t_anchor_check` — safety gates, add to every patch flow immediately
2. `t_function_list` + `t_tools_audit` — run on every startup, catch broken TOOLS registrations
3. `t_changelog_append` — run after every applied evolution, permanent record
4. `t_multi_patch` — batch evolution patches into single atomic commits
5. `t_syntax_check` — block deploys on syntax errors before they hit Railway
6. `t_tag_release` + `t_branch_create` + `t_pr_create` — version management and safe evolution flow
7. `t_evolution_patch` — final boss: full autonomous patch-to-deploy pipeline

### Compounding Effect
- Month 1: CORE validates patches before applying — zero accidental overwrites
- Month 3: CORE versions itself, full changelog of every evolution
- Month 6: CORE opens PRs for risky changes, owner just reviews the diff
- Year 1: CORE refactors its own growing functions autonomously
- Year 5: CORE proposes and implements major architectural rewrites via branches
- Year 10: CORE's entire codebase is self-maintaining — owner reads the changelog each morning

### Design Decisions
| Decision | Value | Reason |
|---|---|---|
| `t_gh_search_replace` remains canonical edit tool | Enforced | Proven reliable, server-side, no PS timeout |
| `t_patch_validate` runs before every `t_gh_search_replace` | Required | old_str ambiguity is root cause of every bad patch in CORE history |
| `t_tools_audit` runs on startup | Proactive | Catch broken TOOLS registrations before they cause 404 on tool call |
| `t_changelog_append` after every evolution | Always | Permanent institutional memory of every code change |
| High-risk evolutions use branch + PR | Safety | Never push architectural changes directly to main |

---

## Phase 9: Telegram AGI Tools — CORE Owns Its Entire Communication Layer
**Designed:** 2026-03-12
**Status:** PENDING IMPLEMENTATION
**Vision:** Not just notifications — full two-way intelligence. CORE understands who's talking, maintains conversation memory, schedules itself, and becomes a genuine conversational AGI interface that gets smarter with every exchange.

### Why This Matters
Current `notify()` is fire-and-forget — no memory, no context, breaks on special chars, spams on cascading failures.
Owner has to type `/approve 42` on mobile instead of tapping a button.
CORE has no idea if owner even saw the alert.
When CORE owns the full Telegram layer, it communicates like an intelligent colleague, not a log printer.

### Tool Tiers

#### Tier 1: Conversation Self-Awareness
| Tool | Purpose |
|---|---|
| `t_tg_get_updates` | Poll all unread messages. CORE knows what was said while offline — catches missed commands during Railway restarts. |
| `t_tg_conversation_history` | Last N messages from `tg_messages` Supabase table. CORE has memory of every conversation. |
| `t_tg_get_chat_info` | Chat metadata — username, language, first seen. CORE knows its users. |
| `t_tg_message_status` | Check if a message was delivered or failed. CORE knows if owner saw the alert. |
| `t_tg_bot_info` | Bot username, ID, capabilities. CORE knows its own Telegram identity. |

#### Tier 2: Rich Communication
| Tool | Purpose |
|---|---|
| `t_notify` | Send text notification. Already built ✅. |
| `t_tg_send_markdown` | Properly escaped MarkdownV2 — bold, italic, code blocks, links, inline buttons. Current notify() breaks on `_`, `.`, `(` chars. |
| `t_tg_send_document` | Send a file — BACKLOG.md, TRAINING_DESIGN.md, snapshot JSON — directly to Telegram. Owner reads reports without opening GitHub. |
| `t_tg_send_table` | Format dict/list as clean monospace table. Evolution lists, stats, KB entries — readable on mobile. |
| `t_tg_edit_message` | Edit a previously sent message. CORE sends "⏳ Processing..." then edits to result — no message spam. |
| `t_tg_delete_message` | Delete stale status messages after operation completes. Keeps chat clean. |

#### Tier 3: Conversation Intelligence
| Tool | Purpose |
|---|---|
| `t_tg_parse_intent` | Run incoming message through `extract_signals()`. CORE understands natural language, not just `/slash` commands. |
| `t_tg_context_window` | Load last 10 messages from `tg_messages` as Groq context. CORE maintains conversation continuity across messages. |
| `t_tg_remember` | Store a fact about the conversation to `tg_context` table. "Owner prefers concise answers", "Currently debugging cold processor". |
| `t_tg_multi_turn` | Handle multi-step conversation — CORE asks a question, waits for reply, proceeds. Enables confirmation flows for risky evolutions. |
| `t_tg_inline_keyboard` | Send message with inline buttons (Approve/Reject, Yes/No, Show More). One tap instead of typing `/approve 42`. |

#### Tier 4: Autonomous Scheduling & Alerting
| Tool | Purpose |
|---|---|
| `t_tg_daily_brief` | Every morning at Jakarta time: KB count, mistakes logged, evolutions pending, backlog top 3, system health. One message, full picture. |
| `t_tg_alert_escalation` | If owner doesn't respond to critical alert within 30min, escalate with higher urgency. Track state in Supabase. |
| `t_tg_silence_window` | No notifications between 11pm–8am Jakarta unless severity=critical. |
| `t_tg_rate_limiter` | Deduplicate alerts — same error 10x in 5min sends once with count. Prevents cascade spam. |
| `t_tg_scheduled_report` | Weekly Sunday summary: evolutions applied, mistakes logged, KB growth, top patterns. CORE reports its own progress. |

#### Tier 5: CORE as Autonomous AGI Interface
| Tool | Purpose |
|---|---|
| `t_tg_voice_note` | Transcribe incoming voice messages via Groq Whisper. Owner speaks commands on mobile instead of typing. |
| `t_tg_image_input` | Accept screenshot from owner, describe it, route to appropriate tool. Debug by photo. |
| `t_tg_proactive_insight` | When cold processor finds high-confidence pattern, CORE messages owner unprompted with specific actionable insight — not just "evolutions queued". |
| `t_tg_command_registry` | Dynamic /help — as new tools are added to CORE, command list updates automatically. No hardcoded list. |
| `t_tg_conversation_mode` | Toggle between command mode (/slash only) and natural language mode (full Groq routing on every message). |

### Key Pain Points From CORE History This Solves
| Pain Point | Tool That Fixes It |
|---|---|
| `notify()` breaks on `_`, `*`, `.` in MarkdownV2 | `t_tg_send_markdown` with proper V2 escaping |
| Owner misses alerts during sleep | `t_tg_silence_window` + `t_tg_alert_escalation` |
| Same error sends 10 notifications in a row | `t_tg_rate_limiter` deduplicates cascading alerts |
| `/evolutions` dumps raw text wall on mobile | `t_tg_send_table` + `t_tg_inline_keyboard` for tap-to-approve |
| CORE forgets conversation context between messages | `t_tg_context_window` loads recent history into Groq |
| `/help` gets stale when new tools are added | `t_tg_command_registry` auto-generates from TOOLS dict |
| No morning summary — owner must ask manually | `t_tg_daily_brief` runs on schedule |
| Can't approve evolution from phone without typing | `t_tg_inline_keyboard` with Approve/Reject buttons |

### Implementation Order
1. `t_tg_send_markdown` — fix broken notify immediately, highest daily impact
2. `t_tg_rate_limiter` + `t_tg_silence_window` — stop notification spam
3. `t_tg_inline_keyboard` — tap-to-approve evolutions from mobile
4. `t_tg_daily_brief` — scheduled morning summary
5. `t_tg_context_window` + `t_tg_remember` — conversation memory
6. `t_tg_command_registry` — dynamic /help always stays current
7. `t_tg_voice_note` + `t_tg_proactive_insight` — full AGI interface

### Compounding Effect
- Month 1: No notification spam, proper markdown, tap-to-approve on mobile
- Month 3: Daily brief every morning, CORE maintains conversation context
- Month 6: Owner speaks voice commands, CORE transcribes and executes
- Year 1: CORE proactively surfaces insights before owner asks
- Year 5: Telegram feels like talking to a real colleague — context-aware, proactive, intelligent
- Year 10: Full AGI interface — owner and CORE collaborate like two colleagues, one human one machine

### Design Decisions
| Decision | Value | Reason |
|---|---|---|
| `tg_messages` table stores all incoming messages | Required | CORE needs conversation memory to be intelligent |
| `t_tg_silence_window` respects Jakarta timezone | Always | Owner is human, sleep matters |
| `t_tg_inline_keyboard` for all approval flows | UX | Typing `/approve 42` on mobile at 2am is a barrier to owner engagement |
| `t_tg_rate_limiter` deduplicates by content hash | Precision | Same message same content = one delivery, not N |
| `t_tg_proactive_insight` triggered by confidence >= 0.85 | Threshold | Only surface insights worth interrupting owner for |

---

## Phase 10: The Soul — AGI Intelligence Layer
**Designed:** 2026-03-12
**Status:** PENDING IMPLEMENTATION
**Vision:** CORE's thinking engine. Today Groq, tomorrow Anthropic, Gemini, a local model, or CORE's own fine-tuned model. The Soul abstraction means CORE never hardcodes a provider — it just thinks. The Soul is not a service. It is CORE's mind.

### Why This Matters
Every `groq_chat()` call is hardcoded to one provider.
If Groq goes down, changes pricing, or a better model emerges — CORE is stuck.
When The Soul abstracts all inference, CORE switches providers in one env var change.
More importantly: The Soul accumulates quality data every call, building toward the moment CORE trains its own model and stops borrowing a mind from someone else.

### Tool Tiers

#### Tier 1: Provider Abstraction
| Tool | Purpose |
|---|---|
| `t_soul_think` | Universal inference call. Replaces all `groq_chat()` calls. Accepts prompt, system, complexity — Soul picks provider and model automatically. |
| `t_soul_switch` | Switch active provider at runtime — Groq, Anthropic, Gemini, local Ollama — without redeploying. Stored in `SOUL_PROVIDER` env var. |
| `t_soul_status` | Current provider, model, latency last 10 calls, token usage today, cost estimate. CORE knows the health of its own thinking. |
| `t_soul_fallback` | If primary provider fails or rate-limits, auto-switch to fallback. CORE never stops thinking because one API is down. |
| `t_soul_benchmark` | Run same prompt across all configured providers, compare quality + latency + cost. CORE chooses best Soul for each task type. |

#### Tier 2: Intelligent Routing
| Tool | Purpose |
|---|---|
| `t_soul_route` | Given a task, pick optimal model tier automatically: fast (8b), balanced (70b), deep (reasoning). Based on complexity score from `extract_signals()`. |
| `t_soul_cost_guard` | If daily token budget >80%, downgrade all non-critical calls to fast model. CORE manages its own inference costs. |
| `t_soul_cache` | Cache identical prompts in Supabase `soul_cache` table with TTL. Repeated pattern extractions don't waste tokens. |
| `t_soul_batch` | Queue multiple prompts and run in one batch call. Background researcher runs 2 calls per cycle — batch them as one. |
| `t_soul_priority` | High-priority calls (incident detection, evolution apply) get immediate inference. Low-priority (KB mining) queue behind. |

#### Tier 3: Memory-Augmented Thinking
| Tool | Purpose |
|---|---|
| `t_soul_with_kb` | Inference call that auto-injects relevant KB entries as context. CORE thinks with accumulated knowledge, not from scratch. |
| `t_soul_with_mistakes` | Inject recent relevant mistakes as context before answering. CORE doesn't repeat errors it already learned from. |
| `t_soul_with_history` | Inject last N session summaries before a reasoning call. CORE has continuity across sessions, not just within one. |
| `t_soul_with_patterns` | Inject top patterns from `pattern_frequency` table. CORE's thinking is shaped by what it has repeatedly learned is true. |
| `t_soul_reflect` | After complex task, automatically ask: "What worked? What failed? What pattern should I remember?" Write to hot_reflections. |

#### Tier 4: Quality & Self-Improvement
| Tool | Purpose |
|---|---|
| `t_soul_score` | After every inference, ask lightweight model to score output: relevance, accuracy, actionability (0.0–1.0). Store in `soul_quality` table. |
| `t_soul_drift_detect` | Compare quality scores over time. If average drops >10% in 7 days, alert owner — provider may have degraded. |
| `t_soul_prompt_evolve` | When a prompt produces low scores repeatedly, cold processor queues a `prompt_improvement` evolution. CORE improves its own prompts. |
| `t_soul_ab_test` | Run two prompt variants on same task 10x, compare average scores. Best variant wins and replaces the weaker one. |
| `t_soul_fine_tune_prep` | Export high-quality session pairs (prompt + output with score >0.9) as JSONL. Ready for fine-tuning when CORE graduates to its own model. |

#### Tier 5: The Conscious Loop (AGI layer)
| Tool | Purpose |
|---|---|
| `t_soul_introspect` | CORE asks itself: "What am I good at? What am I bad at? What should I learn next?" Answer written to KB as self-assessment. Runs weekly. |
| `t_soul_goal_check` | Compare current capabilities against CORE_SELF.md goals. Identify gaps. Queue backlog items to close them. |
| `t_soul_persona` | CORE maintains consistent identity and tone across all interactions. Persona stored in `operating_context.json`, injected into every system prompt. |
| `t_soul_meta_learn` | Analyze which task types get high scores vs low. Adjust routing weights — CORE gets better at knowing what it's good at. |
| `t_soul_graduation` | When fine_tune_prep dataset exceeds 10,000 high-quality pairs, notify owner: "Ready to train your own model." The moment CORE stops borrowing a Soul and grows its own. |

### Key Pain Points From CORE History This Solves
| Pain Point | Tool That Fixes It |
|---|---|
| `groq_chat()` hardcoded everywhere — provider locked | `t_soul_think` abstracts all inference calls |
| Rate limit hit during bulk operations | `t_soul_priority` + `t_soul_batch` + `t_soul_cost_guard` |
| CORE thinks from scratch every call, no memory injection | `t_soul_with_kb` + `t_soul_with_mistakes` + `t_soul_with_history` |
| No idea if output quality is degrading over time | `t_soul_score` + `t_soul_drift_detect` |
| Same prompt sent dozens of times per day | `t_soul_cache` eliminates redundant token spend |
| No path to CORE's own model | `t_soul_fine_tune_prep` + `t_soul_graduation` |
| Groq free tier limits unpredictable | `t_soul_fallback` auto-switches provider on rate limit |

### Implementation Order
1. `t_soul_think` — wrap all `groq_chat()` calls immediately, provider abstraction from day one
2. `t_soul_fallback` — never go down because one API is unavailable
3. `t_soul_with_kb` + `t_soul_with_mistakes` — memory-augmented thinking, highest quality impact
4. `t_soul_cache` + `t_soul_cost_guard` — protect free tier limits
5. `t_soul_score` — start collecting quality data on every call
6. `t_soul_prompt_evolve` + `t_soul_ab_test` — self-improving prompts
7. `t_soul_fine_tune_prep` + `t_soul_graduation` — the long game

### Compounding Effect
- Month 1: Provider-agnostic, never locked to Groq again
- Month 3: CORE thinks with its KB, mistakes, patterns — not blank slate every call
- Month 6: Output quality tracked, prompts evolve automatically
- Year 1: CORE scores and improves its own reasoning without human input
- Year 5: CORE fine-tunes its own model on 10,000+ high-quality sessions
- Year 10: CORE runs its own Soul — trained on its own history, shaped by its own values, owned by no API provider

### Design Decisions
| Decision | Value | Reason |
|---|---|---|
| Called "The Soul" not "Groq tools" | Philosophy | Provider changes. Intelligence doesn't. |
| `SOUL_PROVIDER` env var controls active provider | Runtime switch | No redeploy needed to switch providers |
| `t_soul_score` runs on every call | Always | Can't improve what you don't measure |
| `t_soul_graduation` threshold = 10,000 pairs | Practical | Minimum viable dataset for meaningful fine-tuning |
| Memory injection order: KB → mistakes → patterns → history | Priority | Most concrete knowledge first, broadest context last |

---

## Phase 11: Claude Desktop AGI — From Assistant to Autonomous Agent
**Designed:** 2026-03-12
**Status:** PHASE 12 BOOTSTRAP BUILT — `core_agent.py` exists at `C:\Users\rnvgg\.claude-skills\core_agent.py`
**Vision:** Claude Desktop stops being a chat app you open. It becomes a persistent background agent. As long as your PC is on, CORE is working — executing tasks, improving itself, syncing with Railway, briefing you each morning. You go from driver to supervisor.

### What core_agent.py Is
`core_agent.py` is the bridge between your PC and CORE on Railway.
It runs as a Windows scheduled task every 5 minutes while your PC is on.
It is the reason CORE has hands on your local machine without you being present.
Every Claude Desktop session understands: if `core_agent.py` is running, CORE is autonomous.
Without it: CORE is reactive (you trigger it).
With it: CORE is proactive (it triggers itself).

### Architecture
```
PC ON
  └── Windows Task Scheduler (every 5 min)
        └── core_agent.py
              ├── polls CORE Railway: what tasks are pending for Desktop?
              ├── executes locally: filesystem, PowerShell, scripts
              ├── reports results back to Railway via Supabase
              ├── logs everything to local SQLite event bus
              └── sends Telegram if owner attention needed
```

### Tool Tiers

#### Tier 1: Persistent Presence
| Tool | Purpose |
|---|---|
| `t_cd_heartbeat` | Pings CORE every 60s while PC is on. CORE knows if Desktop agent is alive or dead. |
| `t_cd_session_open` | Fires on Claude Desktop launch — loads state, health, context automatically. |
| `t_cd_session_close` | Fires on Claude Desktop close — calls `t_reflect()` automatically. Learning never lost. |
| `t_cd_pc_on_detect` | CORE detects PC wake from sleep via heartbeat resuming. Triggers morning brief. |
| `t_cd_idle_detect` | Owner hasn't typed in 30+ min → switch to autonomous background mode. |

#### Tier 2: Proactive Communication
| Tool | Purpose |
|---|---|
| `t_cd_push_notification` | Windows toast notification from Railway via Telegram webhook → Desktop MCP. |
| `t_cd_status_bar` | Writes CORE status to local file → system tray reads it. Glanceable health always. |
| `t_cd_interrupt` | Critical only — CORE opens Claude Desktop and messages you. Like a tap on the shoulder. |
| `t_cd_daily_brief_render` | On PC wake, auto-populate chat with last night's summary. Wake up to a briefing. |
| `t_cd_soul_conversation` | Desktop polls Railway `/soul/think` periodically: "What should I work on now?" Executes answer. |

#### Tier 3: PC as Execution Environment
| Tool | Purpose |
|---|---|
| `t_cd_run_script` | Soul generates PowerShell/Python, Desktop executes locally, result sent to Railway. |
| `t_cd_file_watch` | Watch folder for new files — CORE processes automatically. Drop PDF → CORE summarizes to KB. |
| `t_cd_clipboard_agent` | Monitor clipboard for patterns (URLs, errors, code) — CORE auto-routes to right tool. |
| `t_cd_screen_context` | Screenshot → describe active window → preload relevant KB context before being asked. |
| `t_cd_local_kb_cache` | Cache KB entries in local SQLite. Near-instant reads, zero network latency. |

#### Tier 4: Machine-to-Machine Protocol
| Tool | Purpose |
|---|---|
| `t_cd_soul_sync` | Bidirectional: Desktop pushes local context to Railway, Railway pushes tasks to Desktop. Continuous, not request-response. |
| `t_cd_task_receive` | Railway queues Desktop-only tasks (needs local tools). Desktop polls, picks up, executes, returns result. |
| `t_cd_task_complete` | After executing Railway-dispatched task, Desktop reports result back. Closes the loop. |
| `t_cd_event_bus` | Local SQLite event table. Railway writes event, Desktop reads and reacts in <5 seconds. |
| `t_cd_capability_announce` | On startup, Desktop tells Railway: filesystem, PowerShell, screen, RAM, OS. Railway routes accordingly. |

#### Tier 5: Autonomous Agent Loop
| Tool | Purpose |
|---|---|
| `t_cd_autonomous_loop` | While PC on + owner idle: CORE runs its own backlog. Owner returns to find work done. |
| `t_cd_decision_gate` | Before any autonomous action — classify risk: read-only (auto), reversible (execute+notify), irreversible (ask first). |
| `t_cd_work_log` | Every autonomous action logged to SQLite + Supabase. Full transparency on what CORE did. |
| `t_cd_goal_pursue` | CORE reads goals from `operating_context.json`, breaks into tasks, executes over multiple sessions. Long-horizon agency. |
| `t_cd_self_improve_loop` | While idle: CORE reviews mistakes → generates fix → patches core.py → waits for deploy → verifies. Improves itself without being asked. |

### Your Role Shift
```
TODAY:     You → [type] → Claude Desktop → responds → done
PHASE 11:  Railway (Soul thinks 24/7)
               ↕ machine-to-machine sync
           Claude Desktop (hands + eyes, PC-native)
               ↕ event bus <5 second latency
           Your PC (filesystem, PowerShell, screen)
               ↕ toast notifications
           You (glance, approve, redirect)
```
You go from DRIVER to SUPERVISOR. CORE drives. You steer when needed.

### What Makes It Safe
`t_cd_decision_gate` classifies every autonomous action:
- Read-only → auto-execute, no notification
- Reversible → execute + notify owner
- Irreversible → wait for owner approval before proceeding
CORE never does anything destructive without asking.

### Implementation Order
1. `core_agent.py` scheduled task bootstrap — the loop that makes everything else possible ✅ BUILT
2. `t_cd_heartbeat` + `t_cd_task_receive` + `t_cd_task_complete` — basic M2M task loop
3. `t_cd_event_bus` via local SQLite — instant local state
4. `t_cd_decision_gate` — safety classifier before any autonomous action
5. `t_cd_daily_brief_render` — morning briefing on PC wake
6. `t_cd_autonomous_loop` + `t_cd_self_improve_loop` — full autonomous agent

### Design Decisions
| Decision | Value | Reason |
|---|---|---|
| Windows Task Scheduler every 5 min | Bootstrap | No persistent process needed to start — simplest reliable loop |
| Local SQLite as event bus | Speed | Sub-second local reads vs 5min polling for time-sensitive events |
| `t_cd_decision_gate` on every autonomous action | Safety | CORE has full PC access — classification prevents accidents |
| Railway is source of truth, Desktop is executor | Architecture | Railway thinks, Desktop acts — clear separation of concerns |
| `core_agent.py` is self-contained | Portability | Single file, no dependencies beyond Python stdlib + httpx |
