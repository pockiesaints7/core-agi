# CORE v5.0 - Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: ✅ ALL STEPS COMPLETE — CORE v5.0 LIVE

---

## Next Action
CORE v5.0 fully live. Training loop confirmed working.
Cold processor ran autonomously: processed 4 hot_reflections, found 15 patterns.
Next: use CORE for real tasks via Claude Desktop. Feed hot_reflections naturally per session.

---

## Active Tables
knowledge_base, mistakes, sessions, task_queue, changelog,
hot_reflections, cold_reflections, evolution_queue, pattern_frequency

---

## Step Status
- ✅ Step 0: Railway MCP Server + Telegram Bot - COMPLETE 2026-03-11
- ✅ Step 1: Claude Desktop Live Connection - COMPLETE 2026-03-11
- ✅ PRE-STEP 2: Fix t_state() - DONE
- ✅ Step 2: Audit Training Logic - DONE 2026-03-11
- ✅ Step 3: Training Pipeline Implemented - DONE 2026-03-11 (commit fda0388)
- ✅ Step 4: Simulation - DONE 2026-03-11 (commit 66fa36b)
- ✅ Step 5: Deploy & Monitor - COMPLETE 2026-03-11

---

## System State (as of 2026-03-11 session close)
- Railway: live @ https://core-agi-production.up.railway.app
- core.py: commit cc87e5c
- knowledge_base: 346 entries (332 original + 8 build learnings + 6 SOP entries)
- hot_reflections: id 18 unprocessed (SOP injection) — cold will pick up next run
- cold_reflections: 1 real run (id=3) — processed 4 hots, 15 patterns found, 0 evolutions queued
- pattern_frequency: 19 patterns tracked (freq 1-5)
- mistakes: 6 entries from build Steps 0-5
- MCP on CORE: 20 tools active

---

## Injected SOPs (searchable in knowledge_base domain=workflow)
- SOP: Tool Routing — Read Operations (local > github > supabase > railway)
- SOP: Tool Routing — Write Operations (surgical > full rewrite always)
- SOP: Tool Routing — HTTP / External Calls (PowerShell from claude.ai)
- SOP: Session Start Checklist
- SOP: Rate Limit Management (groq, supabase, railway, github free tier rules)
- SOP: When to Use Claude Desktop vs claude.ai for CORE Tasks

---

## Surgical Edit Workflow (claude.ai)
Use Desktop Commander PowerShell:
  $body = @{secret="core_mcp_secret_2026_REINVAGNAR"; path="core.py"; old_str="..."; new_str="..."; message="fix: ..."} | ConvertTo-Json
  Invoke-RestMethod -Uri "https://core-agi-production.up.railway.app/patch" -Method POST -ContentType "application/json" -Body $body

---

## Rules for Claude Desktop Sessions
- NEVER pass `repo` arg ke read_file atau write_file
- NEVER gunakan `query_string` untuk sb_query - gunakan `filters`
- NEVER hardcode step numbers di core.py - pakai get_current_step()
- ALWAYS read-back setelah setiap write sebelum report success
- ALWAYS call get_mistakes(domain=X) sebelum remote write
- ALWAYS update SESSION.md di akhir session kalau ada yang berubah
- Edit file lokal: Desktop Commander:edit_block (surgical)
- Edit GitHub file: gh_search_replace (Claude Desktop) atau /patch (claude.ai)

---

## Context Files
- `operating_context.json` - static: tool rules, schema, tombstone tables
- `SESSION.md` (file ini) - dynamic: active tables, step status, next action
- `TRAINING_DESIGN.md` - pipeline design lengkap (output Step 2)