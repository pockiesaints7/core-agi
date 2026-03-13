# CORE SESSION MASTER
> Last updated: 2026-03-13 | Owner: REINVAGNAR | Version: CORE v5.4 → v6.0 in progress

---

## 1. SESSION START CHECKLIST

**Claude Desktop:**
1. Call `core-agi:session_start` → bootstraps health + counts + last session + mistakes + evolutions
2. Read this file if task registry context needed
3. Check `get_mistakes(domain=X)` before any write in that domain

**claude.ai / mobile:**
1. `web_fetch https://raw.githubusercontent.com/pockiesaints7/core-agi/main/SESSION.md`
2. Use `POST /patch` for any core.py edits (never gh_search_replace from web)
3. Use `github:*` tools for all other file reads/writes

---

## 2. WHAT IS CORE

CORE v5.4 is a Recursive Self-Improvement AGI running 24/7 on Railway.
It learns from every session via a hot→cold reflection pipeline, distills patterns, and evolves its own behavior.
Operated via Claude Desktop (MCP direct, 50 tools), claude.ai (web/mobile), and Telegram (@reinvagnarbot).
Full self-knowledge: see `CORE_SELF.md`. Full tool rules: see `operating_context.json`.
Local v6 plan: `C:\Users\rnvgg\.claude-skills\CORE_v6_plan.md`

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
| `write_file` on core.py | BLOCKED — guard active. Use `gh_search_replace` or `POST /patch` |
| `processed_by_cold` | Use `eq.0` / `eq.1` (integer), NOT `eq.true` / `eq.false` |
| Editing core.py from Desktop | `gh_search_replace` (small) or `github:push_files` (full restore) |
| Editing core.py from claude.ai | `POST /patch` ONLY |
| Structural change | Update CORE_SELF.md FIRST, then operating_context.json, then KB |
| Session end | Always call `session_end` — logs session + hot_reflection in one call |

---

## 5. MASTER TASK REGISTRY (CORE v6.0)

### TASK 1 — Repo Documentation Cleanup
- [x] 1.0 README.md updated (2026-03-13)
- [x] 1.1 SESSION.md rewritten as v6 unified master ← THIS FILE (2026-03-13)
- [ ] 1.2 Slim CORE_SELF.md — remove v5.0 overlap; mcp_tools_count already 50 ✓
- [x] 1.3 Update operating_context.json — already complete from prior session ✓ (verified 2026-03-13)
- [x] 1.4 PROJECT_MODE_DESIGN.md → moved to docs/ (already done)
- [x] 1.5 Delete TRAINING_DESIGN.md ✓ (done prior session)
- [x] 1.6 Delete GOD_MODE_PLAN.md ✓ (done prior session)
- [x] 1.7 Delete MANIFEST.md ✓ (done prior session)
- [x] 1.8 Delete BACKLOG.md (deleted 2026-03-13, was broken 404 stub)
- [x] 1.9 Delete TOOL_AUDIT_TEST.md ✓ (done prior session)
- [x] 1.10 Delete docs/HANDOFF_redeploy_fix.md ✓ (done prior session)
- [x] 1.11 Purge remaining Jarvis OS KB entries — 62 entries deleted 2026-03-13 ✓

### TASK 2 — GOD MODE P2-5A: Architecture Split (Prereq: Task 1)
Split core.py (3092 lines, 157KB) into 4 modules:
- [ ] 2.1 Map exact line ranges per module
- [ ] 2.2 Extract core_github.py
- [ ] 2.3 Extract core_train.py
- [ ] 2.4 Extract core_tools.py
- [ ] 2.5 Create core_main.py (imports from above)
- [ ] 2.6 Smoke test all 50 tools
- [ ] 2.7 Retire core.py monolith → core_legacy.py
- [ ] 2.8 Update docs (entry_point in operating_context.json)

### TASK 3 — Project Mode (Prereq: Task 2)
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
Design docs: docs/ZAPIER_CONNECTIONS.md + ZAPIER_MCP.md
- [ ] 5.1 t_zapier_trigger(zap_id, payload)
- [ ] 5.2 Map active Zapier connections

### TASK 6 — v6.0 Version Stamp 🔒 (LOCKED until Tasks 1-5 done)
Update version strings at lines 2721, 2757, 2771, 2961, 3001, 3082 → "CORE v6.0"

---

## 6. SESSION LOG

| Date | Summary | Key Actions |
|---|---|---|
| 2026-03-11 | v5.0 full launch | Training pipeline live, CORE_SELF.md created, self_sync_check added |
| 2026-03-12 | v5.4 GOD MODE | 50 MCP tools, power tools (session_start/end, blobs, build_status, deploy_and_wait) |
| 2026-03-13 | Cleanup + v6 prep | README updated, repo public, 7 stale files deleted, Jarvis OS KB purged, SESSION.md rewritten |

---

## 7. INCIDENT LOG

| Date | Incident | Resolution |
|---|---|---|
| 2026-03-11 | `write_file` wiped core.py (929→26 lines) | Restored from commit cc87e5c. Guard added blocking write_file on core.py. |
| 2026-03-11 | `import import os` SyntaxError line 55 | Fixed commit 09b370a |
| 2026-03-11 | Wrong env detection — used PowerShell workarounds on Desktop | Mistake #178 logged. Env detection table added to plan. |
| 2026-03-12 | Supabase write rate limit hit (500/hr) during heavy session | Wait 1hr for reset. GOD_MODE_PLAN.md saved to GitHub as backup. |
| 2026-03-12 | PowerShell Railway HTTP calls silently timeout | Never use PowerShell for Railway/GitHub calls. Use MCP tools directly. |
