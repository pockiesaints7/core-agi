# CORE_SYSTEM_MAP.md — Live System Dictionary
> **This file is the single source of truth for all system component names, counts, and identifiers.**
> Auto-updated by CORE whenever structure changes. Never hardcode values from this file in other docs — always fetch it.
> Last updated: 2026-03-14 | Version: CORE v6.0

---

## UPDATE PROTOCOL
**When to update this file:**
- New Supabase table added or dropped
- MCP tool added, renamed, or removed
- GitHub repo renamed or restructured
- Railway service URL or ID changes
- New source file added to architecture
- New interface added (Telegram, web, etc.)

**How to update:**
1. Edit this file via `core-agi:gh_search_replace` or `github:create_or_update_file`
2. Bump `_last_updated` and `_version` at the top
3. Call `changelog_add` to log the structural change
4. Update `CORE_SELF.md` if architecture changed
5. Update `operating_context.json` if table schema changed

---

## 1. IDENTITY

| Key | Value |
|---|---|
| System name | CORE v6.0 — Recursive Self-Improvement AGI |
| Owner | REINVAGNAR |
| Version | v6.0 |
| Primary interface | claude.ai |
| Secondary interfaces | Claude Desktop (MCP direct), Telegram (@reinvagnarbot) |
| Language | Python (FastAPI) |
| AI models | llama-3.3-70b-versatile (primary), llama-3.1-8b-instant (fast) — Groq |

---

## 2. RAILWAY — The Executor

| Key | Value |
|---|---|
| URL | https://core-agi-production.up.railway.app |
| Service name | core-agi |
| Service ID | 48ad55bd-6be2-4d8a-83df-34fc05facaa2 |
| Environment ID | ff3f2a4c-4085-445e-88ff-a423862d00e8 |
| Start command | uvicorn core_main:app (via railway.json) |
| Deploy trigger | Push to GitHub main branch → auto-deploy ~90s |
| MCP tool count | 59 |
| MCP SSE endpoint | https://core-agi-production.up.railway.app/mcp/sse |

### Key endpoints
| Endpoint | Purpose |
|---|---|
| GET / | Health check |
| GET /state | Full system state |
| POST /mcp | MCP tool dispatcher |
| POST /patch | Surgical file edit — {secret, path, old_str, new_str, message} |
| POST /telegram | Telegram webhook |

### Source file architecture
| File | Role |
|---|---|
| core_main.py | FastAPI entry point — routes, /mcp dispatcher, /telegram handler |
| core_tools.py | All 59 MCP tool functions (t_* functions + TOOLS registry) |
| core_train.py | Cold processor, evolution pipeline, hot reflection auto-writer |
| core_config.py | Env vars, constants, RateLimiter, Supabase helpers (sb_get/post/patch/upsert) |
| core_github.py | GitHub read/write helpers (gh_read, gh_write, notify) |
| core_legacy.py | Retired monolith — read-only reference, never edit |

---

## 3. GITHUB — The Source of Truth

| Key | Value |
|---|---|
| Repo | pockiesaints7/core-agi |
| Visibility | Public |
| Primary branch | main |
| Raw base URL | https://raw.githubusercontent.com/pockiesaints7/core-agi/main/ |

### Key files in repo
| File | Type | Purpose |
|---|---|---|
| CORE_SYSTEM_MAP.md | **This file** | Live system dictionary — always fetch before referencing any component |
| SESSION.md | Dynamic | Master task registry, current step, active rules, incident log |
| CORE_SELF.md | Living doc | Architecture diagram, DB schema summary, AI models, file map |
| operating_context.json | Static config | Full table schemas, tool rules, tombstone tables — update on structural change |
| constitution.txt | Immutable | Owner rules — never violate, never modify |
| resource_ceilings.json | Config | Per-service rate limits |
| railway.json | Deploy config | Railway start command and build config |
| requirements.txt | Dependencies | Python package list |

---

## 4. SUPABASE — The Brain

| Key | Value |
|---|---|
| Project ref | qbfaplqiakwjvrtwpbmr |
| REST base URL | https://qbfaplqiakwjvrtwpbmr.supabase.co/rest/v1/ |
| Region | ap-southeast-1 (AWS) |

### Active tables
| Table | Purpose | Read tool | Write tool |
|---|---|---|---|
| knowledge_base | Long-term domain knowledge, SOPs, routing rules | `search_kb`, `ask`, `project_search` | `add_knowledge`, `project_update_kb` |
| mistakes | Error log — prevent repeat failures | `get_mistakes(domain, limit)` | `log_mistake` |
| sessions | Session audit log | `session_start` (last_session field) | `session_end` (auto) |
| hot_reflections | Raw post-session learning — input to cold processor | `sb_query processed_by_cold=eq.0` | `session_end` (auto via Groq) |
| cold_reflections | Distilled cold processor output — read-only | `sb_query` | SYSTEM ONLY |
| evolution_queue | Proposed system improvements | `review_evolutions`, `session_start` | SYSTEM ONLY (cold processor) |
| pattern_frequency | Recurring behavior tracker | `stats`, `synthesize_evolutions` | SYSTEM ONLY |
| sessions | Session log | `session_start` | `session_end` |
| task_queue | Persistent task storage across sessions | `sb_query` | `sb_insert` |
| projects | Registered indexable projects | `project_list` | `project_register` |
| project_context | Prepared project KB context (transient) | `project_context_check` | `project_prepare` (auto) |
| changelog | Permanent change history | `sb_query table=changelog` | `changelog_add` |

### Tombstone tables — NEVER QUERY
These tables have been permanently deleted. Do not query, do not insert.
`playbook`, `memory`, `master_prompt`, `patterns`, `training_sessions`, `training_sessions_v2`,
`training_flags`, `session_learning`, `agent_registry`, `knowledge_blocks`,
`agi_mistakes`, `stack_registry`, `vault_logs`, `vault`

### Live counts (as of last update — fetch fresh via session_start)
| Table | Count |
|---|---|
| knowledge_base | 3,527 entries |
| mistakes | 104 entries |
| sessions | 238 entries |
| hot_reflections | 90 entries |
| evolution_queue | 297 entries |
| task_queue | 123 entries |

> **NOTE:** These counts go stale immediately. Always use `session_start` for live counts.
> This section exists only to give context — never cite these numbers as current.

### sb_query rules
- Parameter is `filters` NOT `query_string`
- Example: `filters="domain=eq.infrastructure"`
- `processed_by_cold`: use `eq.0` / `eq.1` — NOT `eq.true` / `eq.false`
- Unique constraint on knowledge_base: `(domain, topic)` — COMPOSITE, not single topic

---

## 5. LOCAL PC — The Operator Layer

| Key | Value |
|---|---|
| Machine | DESKTOP-QBJ5CUH |
| OS | Windows 10 |
| Username | rnvgg |
| Skills root | C:\Users\rnvgg\.claude-skills\ |
| Python path | C:\Python314\python.exe |

### Key local files
| File | Purpose |
|---|---|
| `services/CREDENTIALS.md` | ALL credentials — read before any auth task, never ask user |
| `CORE_AGI_SKILL_v2.md` | This skill file (v2) |
| `CORE_v6_plan.md` | Active planning doc |
| `ZAPIER_SKILL_GRAPH.md` | 71 Zapier tools with priority rules |
| `projects/PROJECTS.md` | Project registry — mirrors Supabase projects table |
| `projects/equinix-jk1-2.md` | Equinix JK1-2 project index |
| `SCRIPT_REGISTRY.md` | All .ps1/.py scripts — check before writing new ones |
| `project_indexer.py` | Local project indexer — run via Desktop Commander:start_process |
| `selfchat/core_selfchat.py` | Autonomous mode daemon |
| `services/SKILL.md` | Jarvis services layer — CLI patterns per service |
| `pc-context/SKILL-CORE.md` | PC identity, paths, PowerShell rules |

---

## 6. TRAINING PIPELINE — How CORE Evolves

### Data flow
```
session_end
    → hot_reflection written to Supabase (via Groq)
        → cold_processor runs (every 24h or 10 unprocessed hots)
            → patterns extracted → pattern_frequency updated
            → evolutions proposed → evolution_queue populated
            → cold_reflection written
                → Claude reviews via synthesize_evolutions
                    → approved evolutions → code patch → GitHub push → Railway deploy
```

### Thresholds (from core_config.py — fetch live to confirm)
| Config | Value |
|---|---|
| COLD_HOT_THRESHOLD | 10 unprocessed hots triggers cold run |
| COLD_TIME_THRESHOLD | 86400s (24h) time-based trigger |
| PATTERN_EVO_THRESHOLD | 3 occurrences triggers evolution proposal |
| KNOWLEDGE_AUTO_CONFIDENCE | 0.7 |

### Evolution statuses
`pending` → `synthesized` → `approved` / `rejected` → `applied`

---

## 7. RATE LIMITS

| Service | Limit |
|---|---|
| Groq calls | 200/hour |
| Supabase writes | 500/hour |
| GitHub pushes | 20/hour |
| Telegram messages | 30/hour |
| MCP tool calls | 30/minute per session |

---

## 8. RECOVERY PROTOCOL (Railway down)

1. `ping_health` to confirm Railway is down
2. Do NOT call any `core-agi:*` tools — they all route through Railway and will fail
3. Use `github:get_file_contents` to read `SESSION.md` for last known state
4. Use `core-agi:core_py_rollback(commit_sha)` to restore last good commit
5. last_good_commit SHA is in SESSION.md header

---

_Auto-maintained by CORE. Update this file whenever any component name, count, or structure changes._
