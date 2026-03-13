# CORE SESSION MASTER
> Last updated: 2026-03-14 | Owner: REINVAGNAR | Version: CORE v6.0

## Current Step: Task 8.4 ✅ complete — blueprint written as Task 9. Next: 8.5 docs, then pick Task 9 item to execute.

## last_good_commit: 2026-03-14 (post Task 7 — all 50 tools verified green)
> If Railway goes down: use `github:get_file_contents` to read this SHA, restore via `github:push_files`. Do NOT use core-agi: tools when Railway is confirmed down — they all fail simultaneously.

---

## 1. SESSION START CHECKLIST

**Claude Desktop:**
1. Call `core-agi:session_start` → bootstraps health + counts + last session + mistakes + evolutions
2. Read this file if task registry context needed
3. Check `get_mistakes(domain=X)` before any write in that domain

**claude.ai / mobile:**
1. `web_fetch https://raw.githubusercontent.com/pockiesaints7/core-agi/main/SESSION.md`
2. Use `POST /patch` for any source file edits (never gh_search_replace from web)
3. Use `github:*` tools for all other file reads/writes

---

## 2. WHAT IS CORE

CORE v6.0 is a Recursive Self-Improvement AGI running 24/7 on Railway.
It learns from every session via a hot→cold reflection pipeline, distills patterns, and evolves its own behavior.
Operated via Claude Desktop (MCP direct, 50 tools), claude.ai (web/mobile), and Telegram (@reinvagnarbot).
Full self-knowledge: see `CORE_SELF.md`. Full tool rules: see `operating_context.json`.
Architecture: split into core_main.py, core_tools.py, core_train.py, core_github.py, core_config.py.

---

## 3. CURRENT SOP

```
plan → execute → log → reflect → stop
```

- **1 task at a time.** Finish, log, reflect, then move to next.
- **Read before write.** Always. No exceptions.
- **Check mistakes** (domain=X) before any write in that domain.
- **End every Desktop session** with `session_end` tool.
- **Stop at 90% context** → call session_end, standby for next session.
- **When in doubt, do less and ask.**
- **SESSION.md is the single source of truth for all tasks.** When a task item is done, tick it here immediately. No other file tracks task status.

---

## 4. AUTONOMOUS MODE PROTOCOL

When user says "activate autonomous mode":
1. Launch daemon: `C:\Python314\python.exe "C:\Users\rnvgg\.claude-skills\selfchat\core_selfchat.py" --mode watch`
2. Write seed prompt to `C:\Users\rnvgg\.claude-skills\selfchat\prompt.txt`
3. Daemon sends prompt when Claude goes idle (polls for "Stop response" button absence)
   - Before every send: scroll to bottom (click 744,500 → End key → click 979,867) — Claude Desktop does NOT auto-scroll
4. Claude responds → **IMMEDIATELY write next prompt to prompt.txt** ← CRITICAL, loop dies without this
5. Repeat until task complete or user says stop
6. Stop: write `stop` to `status.txt`

---

## 5. ACTIVE RULES

| Rule | Detail |
|---|---|
| `read_file` / `write_file` | OMIT `repo` arg — defaults to pockiesaints7/core-agi |
| `sb_query` | Use `filters` param, NOT `query_string` |
| Source file edits | NEVER hardcode filenames — fetch live from `session_start → architecture.entry_point` |
| Editing source from Desktop | `gh_search_replace` (small) or `github:push_files` (full restore) |
| Editing source from claude.ai | `POST /patch` ONLY |
| `processed_by_cold` | Use `eq.0` / `eq.1` (integer), NOT `eq.true` / `eq.false` |
| Structural change | Update CORE_SELF.md FIRST, then operating_context.json, then KB |
| Session end | Always call `session_end` — logs session + hot_reflection in one call |
| Task done | Tick checkbox in SESSION.md immediately + write result to backlog_update() |
| evolution_queue | Only `knowledge`, `code`, `config` change_types allowed — never `backlog` |
| Railway recovery | If Railway down: read last_good_commit above → restore via github: tools. Never retry core-agi: tools when Railway is confirmed down. |

---

## 6. MASTER TASK REGISTRY (CORE v6.0)

### TASK 1 — Repo Documentation Cleanup ✅
- [x] 1.0 README.md updated (2026-03-13)
- [x] 1.1 SESSION.md rewritten as v6 unified master (2026-03-13)
- [x] 1.2 Slim CORE_SELF.md — version v5.0→v5.4, tools 20→50 ✓ (2026-03-13)
- [x] 1.3 Update operating_context.json ✓ (verified 2026-03-13)
- [x] 1.4 PROJECT_MODE_DESIGN.md → moved to docs/
- [x] 1.5 Delete TRAINING_DESIGN.md ✓
- [x] 1.6 Delete GOD_MODE_PLAN.md ✓
- [x] 1.7 Delete MANIFEST.md ✓
- [x] 1.8 Delete BACKLOG.md (2026-03-13)
- [x] 1.9 Delete TOOL_AUDIT_TEST.md ✓
- [x] 1.10 Delete docs/HANDOFF_redeploy_fix.md ✓
- [x] 1.11 Purge remaining Jarvis OS KB entries — 62 entries deleted 2026-03-13 ✓

### TASK 2 — Architecture Split ✅
Split core.py (3097 lines, 157KB) into 5 modules.
- [x] 2.1 Map exact line ranges per module ✓ (2026-03-13)
- [x] 2.0 core_config.py created ✓ (2026-03-13)
- [x] 2.2 core_github.py created ✓ (2026-03-13)
- [x] 2.3 core_train.py created ✓ (2026-03-13)
- [x] 2.4 core_tools.py created ✓ (2026-03-14)
- [x] 2.5 core_main.py created ✓ (2026-03-14)
- [x] 2.6 Smoke test all 50 tools post-split ✓ (2026-03-14)
- [x] 2.7 core_legacy.py created, Procfile updated → core_main.py ✓ (2026-03-14)
- [x] 2.8 operating_context.json updated: entry_point → core_main.py ✓ (2026-03-14)

### TASK 3 — Project Mode (Prereq: Task 2) 🔄 IN PROGRESS
Design doc: docs/PROJECT_MODE_DESIGN.md
- [ ] 3.1 9 new MCP tools
- [ ] 3.2 Supabase tables: projects + project_context
- [ ] 3.3 Local PROJECTS.md
- [ ] 3.4 Index Equinix JK1-2 as first project

### TASK 4 — Binance/Crypto Integration (Prereq: Task 2)
Design doc: docs/BINANCE_CORE_AGI.md
- [ ] 4.1 Price monitoring thread
- [ ] 4.2 Telegram alert→approve→execute flow
- [ ] 4.3 3 new MCP tools

### TASK 5 — Zapier MCP Integration (Prereq: Task 1)
- [x] 5.0 Scope corrected — KB entry saved (2026-03-13)
- [x] 5.1 Write docs/ZAPIER_MCP.md ✓ (2026-03-13)
- [ ] 5.2 Enable P0 Zapier connections (Gmail, Todoist, Google Calendar, Webhooks)
- [ ] 5.3 Test each P0 connection from Claude Desktop

### TASK 6 — v6.0 Version Stamp 🔒 (LOCKED until Tasks 1-5 done)
Update all version strings → "CORE v6.0" across active modules.

### TASK 7 — Training Pipeline Fix ✅
- [x] 7.1 PATCH `run_cold_processor()` — ALLOWED_EVO_TYPES guard ✓ 2026-03-14
- [x] 7.2 PATCH `apply_evolution()` — delete backlog branch ✓ 2026-03-14
- [x] 7.3 PATCH `backlog_update()` — require result on done ✓ 2026-03-14
- [x] 7.4 NEW `t_changelog_add()` — changelog + Telegram notify ✓ 2026-03-14
- [x] 7.5 PATCH `cold_processor_loop()` — remove backlog auto-apply ✓ 2026-03-14
- [x] 7.6 UPDATE SESSION.md rule table ✓ 2026-03-14
- [x] 7.7 Smoke test + L4 execution — full cycle verified, 9 evolutions executed ✓ 2026-03-14

### TASK 8 — synthesize_evolutions: Claude as CORE Architect (Prereq: Task 3)
> Design finalized 2026-03-14. Claude reads ALL pending evolution_queue entries and thinks as an unconstrained architect — inventing new tools, tables, architecture changes, logic fixes, or entirely new concepts CORE does not know it needs yet. Output is a structured engineering blueprint appended to SESSION.md as a new task chain.

**Why Claude not Groq:** Groq extracts isolated patterns. Claude sees the whole picture, reasons across all signals simultaneously, and can invent what is unthinkable from individual patterns alone.

**What it reads:**
- All evolution_queue where status = pending
- All pattern_frequency high-frequency entries
- Recent cold_reflections (dominant themes)
- Recent gaps_identified from hot_reflections
- Current SESSION.md (existing task context)

**What Claude produces:**
A structured blueprint with concrete task chains — new tools, new tables, architecture changes, logic fixes, wild ideas. Each item tagged: impact (HIGH/MED/LOW), effort (HIGH/MED/LOW), category (new_tool / new_table / architecture / logic_change / wild).

**Relationship with approve/reject — NOT a replacement:**
synthesize_evolutions is the PLANNING GATE before approve/reject, not a substitute.
Workflow: synthesize first (understand big picture + get blueprint) → then bulk approve/reject informed.
Approve still needed: lands Groq KB entries into knowledge_base permanently.
Reject still needed: discards noise. synthesize only produces SESSION.md task chain.

**Evolution status after synthesis:** marked synthesized — acknowledged, not yet approved or rejected.
**Trigger:** Manual only — owner calls it when ready for a planning session.

- [x] 8.1 Add t_synthesize_evolutions() to core_tools.py — fetches all data, assembles full context payload, returns to Claude for unconstrained reasoning
- [x] 8.2 Register in TOOLS dict with architect-level prompt: no constraints, invent freely, think 6 months ahead
- [ ] 8.3 Add status=synthesized handling in evolution_queue flow
- [x] 8.4 Test: call tool, verify Claude produces blueprint with impact/effort matrix + wild ideas section ✓ 2026-03-14
- [ ] 8.5 Update CORE_SELF.md + operating_context.json

---

### TASK 9 — Architect Blueprint (from synthesize_evolutions, 2026-03-14)
> Generated by Claude after reading: 297 synthesized evolutions, 30 top patterns, 10 cold reflections, 0 gaps.
> Core insight: CORE is a world-class *logger*. The next leap is becoming a world-class *actor* — autonomous self-improvement without waiting for Ki to trigger it.

#### 9.A — Pattern Enforcement Engine [architecture | impact: HIGH | effort: MED]
The #1 pattern (190x) is "never assume interface — always verify." CORE keeps re-learning this because it's stored as knowledge, not enforced as a hard check. Build a `pattern_guard` that runs on every session_start and checks the top-10 highest-frequency patterns against a set of runtime assertions. If a session violates a pattern rule, Telegram alert fires before any tools are called.
- [ ] 9.A.1 Define assertion schema for top-10 patterns → store in Supabase `pattern_guards` table
- [ ] 9.A.2 Add `_run_pattern_guards()` call inside `t_session_start()` — runs assertions, returns violations
- [ ] 9.A.3 session_start response includes `guard_violations: []` — Claude sees it immediately
- [ ] 9.A.4 Telegram notify on any HIGH severity violation

#### 9.B — Autonomous Evolution Executor [architecture | impact: HIGH | effort: HIGH]
Right now CORE accumulates 297 evolutions and waits for Ki to call `bulk_apply`. That's a human bottleneck. Build a nightly autonomous executor: cold_processor runs → patterns hit threshold → evolution generated → if confidence ≥ 0.85 AND change_type = knowledge → auto-apply without human approval. Code changes still require approval. This gives CORE true self-improvement on knowledge, while keeping Ki in the loop for code.
- [ ] 9.B.1 Add `auto_apply` column to evolution_queue with threshold config in core_config.py
- [ ] 9.B.2 Patch `apply_evolution()` — knowledge-type evolutions with confidence ≥ 0.85 auto-apply at cold processor end
- [ ] 9.B.3 Telegram summary: "Auto-applied N evolutions overnight. Review: [link]"
- [ ] 9.B.4 Safety gate: max 10 auto-applies per 24h window, hard cap

#### 9.C — Session Quality Scoring [new_table | impact: MED | effort: LOW]
CORE logs sessions but doesn't score them. Add a `session_quality` table that tracks: tools used, mistakes made, patterns violated, evolutions generated, KB entries added, tasks completed. Groq scores the session 0-100 at session_end. Over time this builds a performance graph — CORE can see if it's getting better or worse week over week.
- [ ] 9.C.1 Create `session_quality` Supabase table (session_id, score, tools_used, mistakes, patterns_violated, evolutions_gen, kb_added, tasks_done, notes)
- [ ] 9.C.2 Patch `t_session_end()` to compute + insert quality row via Groq scoring prompt
- [ ] 9.C.3 Add `get_quality_trend(days=7)` read tool — returns weekly score trend
- [ ] 9.C.4 stats() tool includes quality trend in output

#### 9.D — Dead Pattern Pruner [logic_change | impact: MED | effort: LOW]
3,325 KB entries, 190+ patterns — some of these are stale or superseded. Add a `last_reinforced_at` timestamp to `pattern_frequency`. Any pattern not reinforced in 30 days gets flagged as `stale` in a nightly job. Stale patterns are surfaced at session_start so Claude can decide to keep, reinforce, or delete. Prevents KB rot.
- [ ] 9.D.1 Add `last_reinforced_at` + `stale` columns to `pattern_frequency`
- [ ] 9.D.2 Patch cold_processor: update `last_reinforced_at` every time a pattern is incremented
- [ ] 9.D.3 Nightly stale check: mark patterns not reinforced in 30d as stale=true
- [ ] 9.D.4 session_start includes `stale_pattern_count` in response

#### 9.E — Skill Graph [new_table | impact: HIGH | effort: HIGH]
CORE knows facts (KB) and knows rules (patterns). It doesn't know *skills* — compound capabilities built from multiple patterns + tools working together. Add a `skill_graph` table: each skill is a named capability (e.g., "debug Railway deploy failure") with a list of required patterns, tools, and a success rate. CORE builds this graph autonomously by clustering cold_reflections where multiple patterns co-fired. Over time, CORE knows its own skill repertoire.
- [ ] 9.E.1 Design `skill_graph` schema: (id, name, domain, patterns[], tools[], success_rate, last_used)
- [ ] 9.E.2 Create Supabase table + add `t_get_skill_graph()` read tool
- [ ] 9.E.3 Cold processor: cluster co-firing patterns → auto-generate skill entries
- [ ] 9.E.4 session_end: match session actions against skill_graph → update success_rate

#### 9.F — WILD: CORE Self-Prompt Loop [wild | impact: EXTREME | effort: HIGH]
The autonomous mode daemon (Section 4) already exists. The wild idea: CORE generates its own next prompt. At session_end, after Groq reflection, CORE writes a seed_prompt to a `next_session_prompt` state key — a specific, actionable question or task for the next session. When Ki opens Claude Desktop and says "let's go", CORE's first response is built from its own generated prompt, not from a blank slate. CORE becomes the initiator, not just the responder.
- [ ] 9.F.1 Add `next_session_prompt` key to sessions state table
- [ ] 9.F.2 Patch `t_session_end()`: Groq generates 1-sentence seed prompt based on session outcome + open tasks → store in state
- [ ] 9.F.3 `t_session_start()` includes `next_session_prompt` in response — Claude sees it and leads with it
- [ ] 9.F.4 Ki can override by just talking normally — prompt is a suggestion, not a mandate

---

## 7. SESSION LOG

| Date | Summary | Key Actions |
|---|---|------|
| 2026-03-14 | Task 8.4 ✅ — synthesize_evolutions live test + architect blueprint written as Task 9 | session_start → synthesize_evolutions → read SESSION.md SHA → wrote Task 9 blueprint (6 items: 9.A–9.F) |
| 2026-03-13 | Diagnosed and fixed high fail rate in gh_search_replace and  | Patched t_gh_search_replace and t_multi_patch in core_tools.py via multi_patch. Verified readback. Build confirmed success on Railway. |
| 2026-03-13 | Removed all 3 ghost BACKLOG.md gh_write calls from core_trai | identified 3 ghost BACKLOG.md gh_write calls surviving Task 1.8 deletion, attempted gh_search_replace but unicode em-dash blocked match, fetched full core_train.py via github:get_file_contents (+4 more) |
| 2026-03-13 | Task 8.1+8.2 complete — t_synthesize_evolutions added to cor | read current core_tools.py via github:get_file_contents, wrote TOOLS registry entry via gh_search_replace, attempted function body insert via gh_search_replace timed out (+8 more) |
| 2026-03-13 | Designed and registered Task 8 — synthesize_evolutions. Clau | read SESSION.md full, identified correct insertion point for Task 8, designed synthesize_evolutions tool spec (+4 more) |
| 2026-03-13 | Debugged and fixed the full hot reflection pipeline. Three f | read core_train.py auto_hot_reflection, identified missing created_at in session_end call, read core_config.py sb_post returns bool not row (+14 more) |
| 2026-03-13 | Patched run_cold_processor to use Groq for both cold reflect | read core_train.py|added _groq_synthesize_cold — calls GROQ_MODEL with top 15 patterns + domain breakdown + session summaries → meaningful summary_text|added _groq_kb_content — calls GROQ_FAST per pattern that hits threshold → writes proper KB entry content instead of raw pattern string|patched run_cold_processor to call both helpers|patched apply_evolution knowledge branch comment to note change_summary is now Groq-written content|github:push_files|verify_live confirmed live |
| 2026-03-13 | Patched core_train.py: (1) auto_hot_reflection enrichment qu | read core_train.py|diagnosed 3 bugs: no timestamp scoping, max_tokens too low, _extract_real_signal reading all-time|github:push_files patched core_train.py|build_status confirmed pending|verify_live confirmed success |
| 2026-03-13 | Full historical enriched distill session. Read all 4 enrichm | session_start|sb_query mistakes all 100 rows|sb_query changelog all 50 rows|sb_query task_queue all 123 rows|synthesized 8 enriched hots cross-referencing all 4 tables|sb_bulk_insert 8 enriched hots|trigger_cold_processor → 77 patterns|session_end |
| 2026-03-13 | Desktop session — patched auto_hot_reflection in core_train. | session_start|read core_train.py auto_hot_reflection function|designed 4-table enrichment (mistakes/KB/task_queue/changelog)|github:push_files core_train.py with enrichment patch|build_status confirmed success|session_end |
| 2026-03-13 | claude.ai L4 execution session — full L1-L7 pipeline run. Sm | session_start|full supabase sweep all 9 tables|deep scan mistakes+patterns+hots|queued 5 real evolutions (293-297)|10 historical distill hots inserted|trigger_cold_processor → 101 patterns|L4: add_knowledge x5|SESSION.md patched last_good_commit|approve_evolution 293+241+240|reject_evolution 297 with triage note|session_end |
| 2026-03-11 | v5.0 full launch | Training pipeline live, CORE_SELF.md created, self_sync_check added |
| 2026-03-12 | v5.4 GOD MODE | 50 MCP tools, power tools (session_start/end, blobs, build_status, deploy_and_wait) |
| 2026-03-13 | Cleanup + v6 prep | README updated, repo public, 7 stale files deleted, Jarvis OS KB purged, SESSION.md rewritten |
| 2026-03-13 | Architecture split | core.py split into 5 modules, all 50 tools smoke tested, cold processor pattern pipeline fixed |
| 2026-03-14 | Training pipeline design | Skill graph designed, backlog evolutions bulk rejected, SKILL.md updated, Task 7 registered |
| 2026-03-14 | L1-L7 full execution | Smoke test passed, deep scan found 5 real evolutions, historical distill (10 hots, 101 patterns), L4 executed: 5 KB + SESSION.md patch + config triage. Task 7 ✅ |

---

## 8. INCIDENT LOG

| Date | Incident | Resolution |
|---|---|---|
| 2026-03-11 | `write_file` wiped core.py (929→26 lines) | Restored from commit cc87e5c. Guard added. |
| 2026-03-11 | `import import os` SyntaxError line 55 | Fixed commit 09b370a |
| 2026-03-11 | Wrong env detection — PowerShell workarounds on Desktop | Mistake #178 logged. Env detection table added. |
| 2026-03-12 | Supabase write rate limit hit (500/hr) | Wait 1hr for reset. |
| 2026-03-12 | PowerShell Railway HTTP calls silently timeout | Never use PowerShell for Railway/GitHub calls. Use MCP tools directly. |
| 2026-03-14 | core_train.py emitting change_type=backlog evolutions | 67 bulk rejected. Task 7 created to patch source permanently. |
