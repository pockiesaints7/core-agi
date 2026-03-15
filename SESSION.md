# CORE SESSION MASTER
> Last updated: 2026-03-14 | Owner: REINVAGNAR | Version: CORE v6.0

## Current Step: TASK-10: Architect Blueprint v2. Query task_queue for full task definition first.
> Stored in Supabase. Query at session start:
> `sb_query table=task_queue filters=source=eq.core_v6_registry&status=eq.pending&order=priority.desc`

## last_good_commit: 2026-03-14 (post Task 11 — patch_file, validate_syntax, append_to_file live)
> If Railway goes down: use `github:get_file_contents` to read this SHA, restore via `github:push_files`. Do NOT use core-agi: tools when Railway is confirmed down — they all fail simultaneously.

---

## 1. SESSION START CHECKLIST

**Claude Desktop:**
1. Call `core-agi:session_start` → bootstraps health + counts + last session + mistakes + evolutions
2. Query open tasks: `sb_query table=task_queue filters=source=eq.core_v6_registry&status=eq.pending&order=priority.desc`
3. Check `get_mistakes(domain=X)` before any write in that domain

**claude.ai / mobile:**
1. `web_fetch https://raw.githubusercontent.com/pockiesaints7/core-agi/main/SESSION.md`
2. Query open tasks via `sb_query` (same filters above)
3. Use `POST /patch` for any source file edits (never gh_search_replace from web)
4. Use `github:*` tools for all other file reads/writes

---

## 2. WHAT IS CORE

CORE v6.0 is a Recursive Self-Improvement AGI running 24/7 on Railway.
It learns from every session via a hot→cold reflection pipeline, distills patterns, and evolves its own behavior.
Operated via Claude Desktop (MCP direct, 50+ tools), claude.ai (web/mobile), and Telegram (@reinvagnarbot).
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
- **Task registry lives in Supabase task_queue** (source=core_v6_registry). Query it, don't maintain it in markdown.

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
| Source file edits (.py) | Use `patch_file` — has py_compile guard. NEVER use `multi_patch` for .py files. |
| Source file edits (non-.py) | `gh_search_replace` (small) or `multi_patch` (batch) |
| Editing source from claude.ai | `POST /patch` ONLY |
| `processed_by_cold` | Use `eq.0` / `eq.1` (integer), NOT `eq.true` / `eq.false` |
| Structural change | Update CORE_SELF.md FIRST, then operating_context.json, then KB |
| Task status | Before session_end: ALWAYS update active task status in task_queue via sb_patch — in_progress if partial, done if complete. SESSION.md current step is secondary. task_queue is source of truth. |
| Deploy pattern | `patch_file` pushes code -> Railway auto-deploys. Wait 35s -> `build_status()` to confirm. Manual redeploy (no code change): `redeploy()` -> 35s -> `build_status()`. NEVER use `deploy_and_wait` -- deprecated, was a broken polling loop. |
| Session end | Always call `session_end` — logs session + hot_reflection in one call |
| Task done | Update status in task_queue via `sb_query` patch or `update task_queue set status=done` |
| evolution_queue | Only `knowledge`, `code`, `config` change_types allowed — never `backlog` |
| Railway recovery | If Railway down: read last_good_commit above → restore via github: tools. Never retry core-agi: tools when Railway is confirmed down. |
| gh_search_replace on Unicode files | SKIP if file contains em-dashes or non-ASCII. Use github:get_file_contents + github:create_or_update_file directly. |

---

## 6. TASK REGISTRY

**Tasks now live in Supabase `task_queue`, not in this file.**

Query open tasks:
```
sb_query(table="task_queue", filters="source=eq.core_v6_registry&status=eq.pending&order=priority.desc")
```

Query all tasks (including done):
```
sb_query(table="task_queue", filters="source=eq.core_v6_registry&order=priority.desc&limit=20")
```

Task history (Tasks 1–11 registered 2026-03-14):
- TASK-1: Repo Documentation Cleanup ✅
- TASK-2: Architecture Split ✅
- TASK-3: Project Mode ✅
- TASK-4: Binance/Crypto Integration ⬜ (pending, low priority)
- TASK-5: Zapier MCP Integration ✅
- TASK-6: v6.0 Version Stamp ⬜ (locked until Task 4 done)
- TASK-7: Training Pipeline Fix ✅
- TASK-8: synthesize_evolutions — Claude as CORE Architect ✅
- TASK-9: Architect Blueprint v1 (9.A–9.F) ⬜ pending — START HERE
- TASK-10: Architect Blueprint v2 (10.A done, 10.B–10.D pending) ⬜
- TASK-11: Patch Tooling Safety Layer ✅

---

## 7. SESSION LOG

| Date | Summary | Key Actions |
|---|---|------|
| 2026-03-15 | Post-TASK-9.D schema audit. Found and fixed 4 bugs: stale ne | {'action': 'audited all pattern_frequency reads and writes across core_tools.py and core_train.py'}, {'action': 'found 4 bugs: stale never reset on re-appearance; 3 read queries missing stale=eq.false filter'}, {'action': 'patch core_train.py: added stale=False to both upsert paths'} (+4 more) |
| 2026-03-15 | TASK-9.D Dead Pattern Pruner complete. 4 subtasks: (1) last_ | {'action': 'patched cold processor: last_seen updated on every pattern upsert (both existing and new)'}, {'action': 'added _check_stale_patterns() to core_train.py + wired into cold_processor_loop with 24h gate'}, {'action': 'added stale boolean column to pattern_frequency via Supabase management API DDL'} (+4 more) |
| 2026-03-15 | TASK-9.C Session Quality Scoring complete. Confirmed researc | {'action': 'verified researcher fix working -- hot_reflections id=138 source=real generated 2 min after deploy'}, {'action': 'implemented t_get_quality_trend(days) -- daily avg, trend direction, best/worst day'}, {'action': 'wired quality_trend_7d into t_stats() return'} (+5 more) |
| 2026-03-14 | Investigated simulation mode producing zero entries. Root ca | {'action': 'investigated simulation mode -- zero entries ever generated'}, {'action': 'root cause: processed_by_cold=False (bool) not 0 (int) -- both background researcher insert points'}, {'action': 'patch_file core_train.py: fixed both _run_simulation_batch and _extract_real_signal'} (+4 more) |
| 2026-03-14 | TASK-21 fully complete. All 5 subtasks done: (A) add_evoluti | {'action': 'TASK-21.D: backfilled all Section 12 hard rules to KB as proven instruction entries (10 entries)'}, {'action': 'TASK-21.E: updated skill file Layer 2 S2 with complete 3-step evolution persistence flow'}, {'action': 'updated Layer 2 footer timestamp'} (+2 more) |
| 2026-03-14 | TASK-21.A complete. t_add_evolution_rule() built and deploye | {'action': 'patch_file core_tools.py: added t_add_evolution_rule() before TOOLS dict'}, {'action': 'patch_file core_tools.py: registered add_evolution_rule in TOOLS dict'}, {'action': 'verified both commits green: a279f7c37e53 + 45e80a4be8f1'} (+2 more) |
| 2026-03-14 | Owner caught ambiguous wording: NEVER push to GitHub written | {'action': 'filesystem:edit_file skill file Rules 25-26-27: reworded to be specific to CORE_AGI_SKILL_V4.md only, clarified all other CORE files go to GitHub normally'}, {'action': 'edit Layer 2 S2 Write-Back: same clarification added'}, {'action': 'sb_patch KB id=6418: instruction updated with same precision -- local-only applies to skill file only'} |
| 2026-03-14 | Owner corrected a critical mistake from the previous session | {'action': 'deleted wrong TASK-21 (GitHub canonical reference)'}, {'action': 're-inserted TASK-21 corrected: skill file LOCAL PC ONLY, NEVER GitHub, intentional owner control'}, {'action': 'sb_patch KB id=6418: updated instruction to enforce local-PC-only, remove GitHub canonical language'} (+1 more) |
| 2026-03-14 | Corrective session. Owner caught that TASK-21 description an | {'action': 'found wrong file path in TASK-21 description -- said GitHub skills/CORE_AGI_SKILL_V4.md but file is local PC only'}, {'action': 'sb_patch task_queue: fixed TASK-21 description with correct file locations and FILE LOCATION FACTS section'}, {'action': 'log_mistake: writing wrong file path into task description'} (+1 more) |
| 2026-03-14 | Owner identified the most fundamental AGI design flaw: CORE  | {'action': 'task_add TASK-21: Persistent Evolution Engine -- priority 9, full subtask detail, context for future sessions'}, {'action': 'add_knowledge: AGI Rule #1 -- evolution must write to persistent storage, not chat promises'}, {'action': 'skill file updated with TASK-21 context (Layer 1 Rule 28 synthesize_evolutions, patch SOP Rules 22+23)'} |
| 2026-03-14 | Corrective session. Two mistakes caught by owner: (1) skippe | {'action': 'log_mistake: skipped TOOLS dict patch after refactor'}, {'action': 'log_mistake: violated read-before-patch SOP -- assumed indentation'}, {'action': 'gh_read_lines lines 2414-2416 -- read exact TOOLS entry before patching'} (+3 more) |
| 2026-03-14 | Two architectural changes this session. TASK-17: auto-apply  | {'action': 'patch core_train.py: TASK-17 auto-apply gate in run_cold_processor'}, {'action': 'patch core_tools.py: synthesize_evolutions stripped to pure data fetcher'}, {'action': 'changelog_add x2'} (+2 more) |
| 2026-03-14 | Rewrote synthesize_evolutions from a context-assembler-for-C | {'action': 'rewrote t_synthesize_evolutions: Groq acts as architect server-side, reads 5 signal sources, generates 3-8 structured JSON tasks, inserts into task_queue source=core_v6_registry, marks evolutions synthesized, sends Telegram notify'}, {'action': 'updated TOOLS dict desc for synthesize_evolutions: perm READ->WRITE, desc reflects new behavior'}, {'action': 'changelog_add'} |
| 2026-03-14 | Full evolution pipeline cycle completed: 2 pending evolution | {'action': 'synthesize_evolutions: 2 pending evolutions synthesized, architect blueprint produced'}, {'action': 'applied evolution 299 to KB: core_agi.code_patching / read_before_patch_rule'}, {'action': 'applied evolution 300 to KB: core_agi.training / cold_reflections_audit_trail'} (+4 more) |
| 2026-03-14 | Fixed all 3 cold processor bugs that were silently killing t | {'action': 'diagnosed 3 cold processor bugs: pattern fragmentation, auto_applied permanent block, 200-char key truncation'}, {'action': 'appended _groq_cluster_patterns() to core_train.py -- semantic deduplication before counting'}, {'action': 'patched run_cold_processor: wired clustering, raised key truncation to 500, added milestone re-queuing at freq 10/25/50/100'} (+3 more) |
| 2026-03-14 | Code audit found 1 confirmed active bug in auto_hot_reflecti | {'action': 'audited auto_hot_reflection payload against actual DB column schema for all fields'}, {'action': 'found gaps_identified text[] type mismatch -- Groq string silently coerced to null by PostgREST on every write since launch'}, {'action': 'patched core_train.py: wrap gaps_identified string in list before sb_post'} (+1 more) |
| 2026-03-14 | Retroactively applied what Groq would have produced if the e | {'action': 'backfilled hot_reflections 111-124 with Groq-simulated enriched patterns -- 4-5 patterns per session replacing the 1-3 seed-only patterns that existed due to the anchor bug'}, {'action': 'cold processor run registered: 3 evolutions queued (ids 298-300), cold_reflections row id=85 written, pattern_frequency upserted for 3 patterns'}, {'action': 'all 14 hot_reflections marked processed_by_cold=1 correctly'} |
| 2026-03-14 | Critical training pipeline fix. auto_hot_reflection was enri | {'action': 'diagnosed enrichment anchor bug: session_ts was set to session_end call time, all 4 enrichment queries returned empty every session'}, {'action': 'patched core_train.py: anchor now fetches last hot_reflection created_at from DB -- full delta since last scan'}, {'action': 'fallback chain: last hot_reflection -> session created_at -> 24h ago'} (+1 more) |
| 2026-03-14 | Quick fix: every session_end was triggering an unnecessary R | {'action': 'diagnosed root cause: session_end gh_write SESSION.md had no [skip ci] -- every session close triggered Railway redeploy'}, {'action': 'patched core_tools.py via GitHub API (PowerShell): added [skip ci] to SESSION.md commit message'}, {'action': 'changelog_add: fix skip ci'} |
| 2026-03-14 | TASK-16 fully complete. All 6 subtasks done: 16.A brain tabl | {'action': 'verified 16.B _reconcile_executor_files() already deployed and working'}, {'action': 'verified 16.C _reconcile_skeleton_docs() already deployed and working'}, {'action': 'confirmed 16.A/B/C all wired in t_system_map_scan at lines 622-629'} (+3 more) |
| 2026-03-14 | Two owner directives executed. (1) Taught CORE skill file lo | {'action': 'kb_update domain=system.config topic=skill_file_location -- skill file path + upload protocol'}, {'action': 'kb_update domain=system.zapier topic=tool_priority_and_connections -- Zapier rank 1, active connections, config URL'}, {'action': 'kb_update domain=system.config topic=tool_namespace_priority -- full 7-rank priority table'} (+3 more) |
| 2026-03-14 | Two major fixes this session. (1) TASK-16.A complete: _recon | {'action': 'patched core_tools.py: replace deploy_and_wait 120s polling loop with thin build_status wrapper'}, {'action': 'patched SESSION.md: updated deploy SOP rule'}, {'action': 'kb_update domain=core_agi.deploy topic=deploy_sop with new SOP'} (+8 more) |
| 2026-03-14 | Made system_map executor.tool layer fully automatic and vers | session_start, build confirmed success, patch t_system_map_scan — auto-reconcile tools at session_end (+6 more) |
| 2026-03-14 | Session focused on Layer 3 S-END-5 system_map reconciliation | session_start, S-START-2 tool count verified: 71 live = 71 Telegram, task_queue statuses audited and corrected by UUID (+2 more) |
| 2026-03-14 | Fixed all 4 Telegram notification issues: (1) evolution coun | read core_main.py boot notification, diagnosed 4 issues: evo count wrong, task count wrong, unprocessed reflections noise, cold processor config noise, researcher cycle always-notifying with raw bools, patched get_system_counts in core_main.py — evo by status, task pending only (+7 more) |
| 2026-03-14 | TASK-13.H validation complete. validate_syntax: PASS (false  | validate_syntax, build_status check, get_mistakes full fields test (+9 more) |
| 2026-03-14 | Post-session cleanup. Owner caught that SESSION.md contains  | identified SESSION.md scope drift, added 2 KB entries on SESSION.md purpose and anti-patterns, queued TASK-15 SESSION.md cleanup in task_queue (+1 more) |
| 2026-03-14 | TASK-13 A→G complete. Full core_tools.py overhaul: A.2+A.3 p | session_start, read CORE AGI skill, 13.A.2 t_project_get array fix (+18 more) |
| 2026-03-14 | TASK-12 complete. Patched _extract_real_signal() in core_tra | session_start|read core_train.py functions via core_py_fn|audited _extract_real_signal _run_simulation_batch _ingest_public_sources background_researcher|confirmed 12.A/12.B/12.D already implemented|patched _extract_real_signal via patch_file (4 patches, old_str/new_str keys)|deploy_and_wait success commit 0ff78fb53699|changelog_add logged|queried task_queue by source=core_v6_registry to find TASK-12 UUID|sb_patch status=done with correct UUID after catching typo|session_end |
| 2026-03-14 | Session ended early. Caught and logged mistake: used PowerSh | log_mistake PowerShell syntax check, sb_insert TASK-12 into task_queue with full subtask spec |
| 2026-03-14 | Researched and documented Groq/llama-3.3 hard limits: knowle | researched Groq/llama-3.3 hard limits and capabilities, web_search confirmed knowledge cutoff December 2023 and no internet access, set_simulation with explicit 3-angle evolution engine instruction (+1 more) |
| 2026-03-14 | CORE whole-system documentation of task management architect | add_knowledge x3 (SESSION.md scope, task lifecycle, task_queue schema) (+6 more) |
| 2026-03-14 | SESSION.md task registry migrated to Supabase task_queue. Al | sb_bulk_insert 11 tasks into task_queue, SESSION.md rewritten to SOP-only, changelog logged (+1 more) |
| 2026-03-14 | SESSION.md refactored — task registry moved to Supabase task_queue | sb_bulk_insert 11 tasks, SESSION.md slimmed to SOP-only, session_end |
| 2026-03-14 | Task 11 complete. patch_file, validate_syntax, append_to_file live | patch_file+validate_syntax+append_to_file added to core_tools.py, KB entries, skill files updated |
| 2026-03-14 | Task 8 complete. synthesize_evolutions live-tested | synthesize_evolutions called, Task 9 blueprint produced, CORE_SELF.md updated |
| 2026-03-14 | Task 10.A complete — Unicode pre-flight guard deployed | Patched t_gh_search_replace + t_multi_patch, KB entry, changelog |
| 2026-03-14 | Task 7 complete — training pipeline fix | 7 patches, smoke test, 9 evolutions executed |
| 2026-03-14 | Layer 2 Behavior Pipeline designed and shipped | CORE_AGI_SKILL_V4.md 860 lines, integrated into Claude Desktop |
| 2026-03-13 | Task 5 complete — Zapier P0 connections tested | Gmail, Calendar, Todoist, Webhooks all green |
| 2026-03-13 | Task 3 foundation complete — Project Mode built | 9 MCP tools, projects + project_context tables, PROJECTS.md |
| 2026-03-13 | Task 2 complete — Architecture split | core.py → 5 modules, 50 tools smoke tested |
| 2026-03-13 | Task 1 complete — Repo cleanup | README, 7 files deleted, 62 KB entries purged |
| 2026-03-11 | v5.0 full launch | Training pipeline live, CORE_SELF.md created |
| 2026-03-12 | v5.4 GOD MODE | 50 MCP tools, power tools live |

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