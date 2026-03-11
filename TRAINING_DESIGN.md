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
