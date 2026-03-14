# CORE SESSION MASTER
> Last updated: 2026-03-14 | Owner: REINVAGNAR | Version: CORE v6.0

## Current Step: TASK-13 — Full Tools Evolution. Start at 13.A (critical crashes). Execute in order: A→B→C→D→E→F→G→H→I. patch_file for all .py changes. Note: 13.A.1 (notify_owner) NOT needed — alias confirmed at core_tools.py line 33. Start at 13.A.2 (project_get array crash).
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
| Deploy pattern | ALWAYS: `redeploy()` → `sleep 30s` → `deploy_and_wait(reason='sha:COMMIT')`. NEVER call deploy_and_wait alone — it no longer triggers redeploy. deploy_and_wait is poll-only. |
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