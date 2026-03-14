# CORE SESSION MASTER
> Last updated: 2026-03-14 | Owner: REINVAGNAR | Version: CORE v6.0

## Current Step: Task 10.A ✅. Indexer running PID 736 (equinix-jk1-2). CORE_AGI_SKILL.md updated with Document & File Map. Next: verify indexer completed, then Task 10.B (project_search test) or Task 10.C (session_end auto-step).

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
| gh_search_replace on Unicode files | SKIP if file contains em-dashes or non-ASCII. Use github:get_file_contents + github:create_or_update_file directly. |

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

### TASK 3 — Project Mode ✅
Design doc: docs/PROJECT_MODE_DESIGN.md
- [x] 3.1 9 new MCP tools (2026-03-13)
- [x] 3.2 Supabase tables: projects + project_context (2026-03-13)
- [x] 3.3 Local PROJECTS.md (2026-03-13)
- [x] 3.4 Equinix JK1-2 registered via project_register ✓ (2026-03-14)
- [x] 3.5 operating_context.json v2.5 — changelog enriched, projects + project_context added to active_tables ✓ (2026-03-14)

### TASK 4 — Binance/Crypto Integration (Prereq: Task 2)
Design doc: docs/BINANCE_CORE_AGI.md
- [ ] 4.1 Price monitoring thread
- [ ] 4.2 Telegram alert→approve→execute flow
- [ ] 4.3 3 new MCP tools

### TASK 5 — Zapier MCP Integration (Prereq: Task 1) ✅
- [x] 5.0 Scope corrected — KB entry saved (2026-03-13)
- [x] 5.1 Write docs/ZAPIER_MCP.md ✓ (2026-03-13)
- [x] 5.2 Enable P0 Zapier connections (Gmail, Todoist, Google Calendar, Webhooks)
- [x] 5.3 Test each P0 connection from Claude Desktop

### TASK 6 — v6.0 Version Stamp 🔒 (LOCKED until Tasks 4+5 done)
Update all version strings → "CORE v6.0" across active modules.

### TASK 7 — Training Pipeline Fix ✅
- [x] 7.1 PATCH `run_cold_processor()` — ALLOWED_EVO_TYPES guard ✓ 2026-03-14
- [x] 7.2 PATCH `apply_evolution()` — delete backlog branch ✓ 2026-03-14
- [x] 7.3 PATCH `backlog_update()` — require result on done ✓ 2026-03-14
- [x] 7.4 NEW `t_changelog_add()` — changelog + Telegram notify ✓ 2026-03-14
- [x] 7.5 PATCH `cold_processor_loop()` — remove backlog auto-apply ✓ 2026-03-14
- [x] 7.6 UPDATE SESSION.md rule table ✓ 2026-03-14
- [x] 7.7 Smoke test + L4 execution — full cycle verified, 9 evolutions executed ✓ 2026-03-14

### TASK 8 — synthesize_evolutions: Claude as CORE Architect ✅
> Design finalized 2026-03-14. Claude reads ALL pending evolution_queue entries and thinks as an unconstrained architect — inventing new tools, tables, architecture changes, logic fixes, or entirely new concepts CORE does not know it needs yet. Output is a structured engineering blueprint appended to SESSION.md as a new task chain.

- [x] 8.1 Add t_synthesize_evolutions() to core_tools.py ✓ 2026-03-14
- [x] 8.2 Register in TOOLS dict with architect-level prompt ✓ 2026-03-14
- [x] 8.3 Add status=synthesized handling in evolution_queue flow ✓ 2026-03-14
- [x] 8.4 Test: call tool, verify Claude produces blueprint ✓ 2026-03-14
- [x] 8.5 Update CORE_SELF.md + operating_context.json ✓ 2026-03-14

---

### TASK 9 — Architect Blueprint (from synthesize_evolutions, 2026-03-14)
> Generated by Claude after reading: 297 synthesized evolutions, 30 top patterns, 10 cold reflections, 0 gaps.
> Core insight: CORE is a world-class *logger*. The next leap is becoming a world-class *actor* — autonomous self-improvement without waiting for Ki to trigger it.

#### 9.A — Pattern Enforcement Engine [architecture | impact: HIGH | effort: MED]
- [ ] 9.A.1 Define assertion schema for top-10 patterns → store in Supabase `pattern_guards` table
- [ ] 9.A.2 Add `_run_pattern_guards()` call inside `t_session_start()`
- [ ] 9.A.3 session_start response includes `guard_violations: []`
- [ ] 9.A.4 Telegram notify on any HIGH severity violation

#### 9.B — Autonomous Evolution Executor [architecture | impact: HIGH | effort: HIGH]
- [ ] 9.B.1 Add `auto_apply` column to evolution_queue with threshold config
- [ ] 9.B.2 Patch `apply_evolution()` — knowledge-type, confidence ≥ 0.85 auto-apply at cold processor end
- [ ] 9.B.3 Telegram summary: "Auto-applied N evolutions overnight."
- [ ] 9.B.4 Safety gate: max 10 auto-applies per 24h window

#### 9.C — Session Quality Scoring [new_table | impact: MED | effort: LOW]
- [ ] 9.C.1 Create `session_quality` Supabase table
- [ ] 9.C.2 Patch `t_session_end()` to compute + insert quality row via Groq
- [ ] 9.C.3 Add `get_quality_trend(days=7)` read tool
- [ ] 9.C.4 stats() includes quality trend

#### 9.D — Dead Pattern Pruner [logic_change | impact: MED | effort: LOW]
- [ ] 9.D.1 Add `last_reinforced_at` + `stale` columns to `pattern_frequency`
- [ ] 9.D.2 Patch cold_processor: update `last_reinforced_at` on every increment
- [ ] 9.D.3 Nightly stale check: mark patterns not reinforced in 30d
- [ ] 9.D.4 session_start includes `stale_pattern_count` in response

#### 9.E — Skill Graph [new_table | impact: HIGH | effort: HIGH]
- [ ] 9.E.1 Design `skill_graph` schema: (id, name, domain, patterns[], tools[], success_rate, last_used)
- [ ] 9.E.2 Create Supabase table + `t_get_skill_graph()` read tool
- [ ] 9.E.3 Cold processor: cluster co-firing patterns → auto-generate skill entries
- [ ] 9.E.4 session_end: match session actions against skill_graph → update success_rate

#### 9.F — WILD: CORE Self-Prompt Loop [wild | impact: EXTREME | effort: HIGH]
- [ ] 9.F.1 Add `next_session_prompt` key to sessions state table
- [ ] 9.F.2 Patch `t_session_end()`: Groq generates 1-sentence seed prompt → store in state
- [ ] 9.F.3 `t_session_start()` includes `next_session_prompt` in response
- [ ] 9.F.4 Ki can override by just talking normally — prompt is a suggestion, not a mandate

---

### TASK 10 — Architect Blueprint v2 (from synthesize_evolutions, 2026-03-14 session 2)
> Generated after reading: 0 pending evolutions (all synthesized), 30 top patterns, 10 cold reflections.
> New signal this round: the em-dash encoding failure reveals a deeper pattern — CORE's file editing toolchain has a hidden fragility layer that causes silent failures and wastes tool calls. Also: project_context table exists but has never actually been used (no indexing run on Equinix JK1-2). The project mode is built but inert.

#### 10.A — File Edit Safety Layer [architecture | impact: HIGH | effort: LOW]
The gh_search_replace / multi_patch failure on Unicode files is a recurring silent killer (this session: 4 failed tool calls, forced full-file push). Add a pre-flight Unicode detector in `t_gh_search_replace` and `t_multi_patch`: if the file contains non-ASCII characters, return a warning immediately with the recommendation to use `github:get_file_contents + github:create_or_update_file`. Zero code complexity, eliminates an entire class of silent failures permanently.
- [x] 10.A.1 Patch `t_gh_search_replace()` in core_tools.py: after fetching file, scan for non-ASCII chars → if found and old_str contains non-ASCII, return `{"ok": false, "error": "unicode_file — use get_file_contents + create_or_update_file instead", "hint": "file contains non-ASCII characters"}`
- [x] 10.A.2 Same patch for `t_multi_patch()`
- [x] 10.A.3 Add rule to ACTIVE RULES table in SESSION.md (done this session ✓)
- [x] 10.A.4 Add KB entry: "CORE File Edit — Unicode Safety Rule"

#### 10.B — Project Indexer [new_tool | impact: HIGH | effort: MED]
Equinix JK1-2 is registered but `last_indexed` is null and `project_context` table is empty — the project system built in Task 3 has never actually run. Build `project_index` tool: reads all files in `folder_path`, chunks content, writes KB entries tagged with `project_id`, updates `last_indexed`. This is the missing bridge between "project registered" and "CORE can answer questions about it."
- [ ] 10.B.1 Build `t_project_index(project_id)` in core_tools.py: reads folder_path from projects table, walks directory tree, extracts text from .pdf/.xlsx/.docx/.txt files
- [ ] 10.B.2 Chunk content → `add_knowledge` entries with domain=`project:{project_id}`, tags=[project_id]
- [ ] 10.B.3 Call `project_update_index` at end to stamp `last_indexed`
- [ ] 10.B.4 Telegram notify: "Indexed {N} files for project {name}"
- [ ] 10.B.5 Test on Equinix JK1-2

#### 10.C — Stale Session Step Auto-Updater [logic_change | impact: MED | effort: LOW]
Current Step in SESSION.md is often stale across sessions — it said "Task 3 in progress" for multiple sessions even after 3.1/3.2/3.3 were done. Add logic in `t_session_end()`: after writing SESSION.md, compare completed_tasks param against the task registry and auto-suggest the correct next Current Step. Claude writes it, Groq doesn't need to understand the full task graph — just pattern-match on what was ticked.
- [ ] 10.C.1 Patch `t_session_end()`: parse `completed_tasks` arg, generate `new_step` suggestion based on what's still open in SESSION.md
- [ ] 10.C.2 If `new_step` already provided by caller, use that. If not, auto-generate from task scan.
- [ ] 10.C.3 Test: call session_end without new_step, verify auto-generated step is correct

#### 10.D — WILD: CORE's Own Mistake Predictor [wild | impact: HIGH | effort: MED]
103 mistakes logged, 30 patterns extracted — CORE has enough signal to start predicting its own failures before they happen. Before any write operation (gh_search_replace, sb_insert, push_files), run a lightweight Groq call: "Given this action + these top-5 domain mistakes, what's the most likely failure?" If confidence > 0.7, surface the prediction as a warning in the tool response. CORE becomes self-aware of its own failure modes in real time.
- [ ] 10.D.1 Build `_predict_failure(action_description, domain)` helper in core_tools.py — calls Groq fast model with top-5 domain mistakes as context
- [ ] 10.D.2 Wire into `t_gh_search_replace`, `t_multi_patch`, `t_sb_insert` as optional pre-flight
- [ ] 10.D.3 Return prediction in tool response as `{"warning": "...", "confidence": 0.8}` — non-blocking
- [ ] 10.D.4 Log prediction accuracy back to mistakes table to improve over time

---

## 7. SESSION LOG

| Date | Summary | Key Actions |
|---|---|------|
| 2026-03-14 | Updated CORE_AGI_SKILL.md with full Document & File Map sect | session_start|read SKILL.md|read source files (core_tools.py, core_config.py, core_train.py)|synthesize skill graph diagram|study all .md files on local PC + GitHub|update CORE_AGI_SKILL.md with full Document & File Map|add_knowledge credentials vault location|session_end |
| 2026-03-14 | Task 10.A complete — Unicode pre-flight guard deployed to t_ | session_start|project_list (Task 3.4 already done)|gh_search_replace x3 failed Unicode|github:get_file_contents+create_or_update_file operating_context.json v2.5|log_mistake em-dash encoding|synthesize_evolutions → Task 10 blueprint|SESSION.md updated (Task 3 ticked, Task 10 added)|core_py_fn t_gh_search_replace|core_py_fn t_multi_patch|multi_patch Unicode pre-flight both functions|build_status pending|add_knowledge Unicode Safety Rule|changelog_add v6.0.1 |
| 2026-03-14 | Task 3 ✅ complete. operating_context.json v2.5 pushed. Mistake logged (em-dash Unicode). Task 10 synthesize blueprint written. | session_start|project_list (3.4 already done)|gh_search_replace (failed Unicode)|github:get_file_contents+create_or_update_file operating_context.json v2.5|log_mistake em-dash encoding|synthesize_evolutions|SESSION.md updated Task 10 + Task 3 ticked |
| 2026-03-13 | Task 3 foundation complete. Built 9 project MCP tools (proje | session_start|read PROJECT_MODE_DESIGN.md full|checked mistakes domain=infrastructure|multi_patch 9 project tools into core_tools.py|multi_patch /project handler into core_main.py|core_py_validate — core_main.py clean, core_tools.py false positives confirmed pre-existing|build_status — both commits green|webhooks GET /state to verify deploy|PROJECTS.md created locally|multi_patch operating_context.json v2.4 (3/4 applied)|add_knowledge x2|changelog_add |
| 2026-03-13 | Completed Zapier full skill enumeration and documentation. D | read ZAPIER_MCP.md|tool_search x6 for all Zapier domains|discovered 8 Gemini tools undetected in Task 5.3|built ZAPIER_SKILL_GRAPH.md (71 tools, 15 compound skills)|added tool priority system to CORE_AGI_SKILL.md|saved both locally to C:\Users\rnvgg\.claude-skills\|pushed ZAPIER_SKILL_GRAPH.md to docs/ in repo|add_knowledge tool priority system|changelog_add |
| 2026-03-13 | Task 5 complete. Tested all 4 P0 Zapier connections: Gmail ( | read ZAPIER_MCP.md|tool_search for all 4 P0 tools|gmail_find_email test ✅|google_calendar_find_events test ✅|todoist_find_task test ✅|webhooks_by_zapier_post test ✅|changelog_add logged |
| 2026-03-13 | Task 8 complete. synthesize_evolutions live-tested — confirm | synthesize_evolutions called|Claude produced Task 9 architect blueprint|SESSION.md updated with Task 9 (6 items) + 8.4 ticked|CORE_SELF.md rewritten: v6.0, 5-module arch, 50 tools (+4 more) |
| 2026-03-14 | Task 8.4 ✅ — synthesize_evolutions live test + architect blueprint written as Task 9 | session_start → synthesize_evolutions → read SESSION.md SHA → wrote Task 9 blueprint (6 items: 9.A–9.F) |
| 2026-03-13 | Diagnosed and fixed high fail rate in gh_search_replace and  | Patched t_gh_search_replace and t_multi_patch in core_tools.py via multi_patch. Verified readback. Build confirmed success on Railway. |
| 2026-03-13 | Removed all 3 ghost BACKLOG.md gh_write calls from core_trai | identified 3 ghost BACKLOG.md gh_write calls surviving Task 1.8 deletion, attempted gh_search_replace but unicode em-dash blocked match, fetched full core_train.py via github:get_file_contents (+4 more) |
| 2026-03-13 | Task 8.1+8.2 complete — t_synthesize_evolutions added to cor | read current core_tools.py via github:get_file_contents, wrote TOOLS registry entry via gh_search_replace, attempted function body insert via gh_search_replace timed out (+8 more) |
| 2026-03-13 | Designed and registered Task 8 — synthesize_evolutions. Clau | read SESSION.md full, identified correct insertion point for Task 8, designed synthesize_evolutions tool spec (+4 more) |
| 2026-03-13 | Debugged and fixed the full hot reflection pipeline. Three f | read core_train.py auto_hot_reflection, identified missing created_at in session_end call, read core_config.py sb_post returns bool not row (+14 more) |
| 2026-03-13 | Patched run_cold_processor to use Groq for both cold reflect | read core_train.py|added _groq_synthesize_cold|added _groq_kb_content|patched run_cold_processor|patched apply_evolution|github:push_files|verify_live confirmed live |
| 2026-03-13 | Patched core_train.py: (1) auto_hot_reflection enrichment qu | read core_train.py|diagnosed 3 bugs|github:push_files patched core_train.py|build_status confirmed pending|verify_live confirmed success |
| 2026-03-13 | Full historical enriched distill session. | session_start|sb_query mistakes+changelog+task_queue|synthesized 8 enriched hots|sb_bulk_insert 8 hots|trigger_cold_processor → 77 patterns|session_end |
| 2026-03-13 | Desktop session — patched auto_hot_reflection. | session_start|read core_train.py|github:push_files core_train.py|build_status confirmed|session_end |
| 2026-03-13 | claude.ai L4 execution session. | session_start|full supabase sweep|queued 5 real evolutions|10 historical hots|trigger_cold_processor → 101 patterns|approve/reject evolutions|session_end |
| 2026-03-11 | v5.0 full launch | Training pipeline live, CORE_SELF.md created, self_sync_check added |
| 2026-03-12 | v5.4 GOD MODE | 50 MCP tools, power tools live |
| 2026-03-13 | Cleanup + v6 prep | README, 7 files deleted, KB purged, SESSION.md rewritten |
| 2026-03-13 | Architecture split | core.py → 5 modules, 50 tools smoke tested |
| 2026-03-14 | Training pipeline design | Skill graph, Task 7 registered, L1-L7 executed |

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
| 2026-03-14 | gh_search_replace silently fails on files with em-dash (U+2014) | Use github:get_file_contents + create_or_update_file. Rule added to Section 5. Mistake logged. |