# CORE v5.0 - Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: ✅ ALL STEPS COMPLETE — CORE v5.0 LIVE

---

## Next Action
CORE v5.0 fully live. Steps 0-5 complete 2026-03-11. Training pipeline active.
Retroactive learnings injected: 8 KB entries, 6 mistakes, 4 hot_reflections (Steps 0-5).
Next: use CORE for real tasks via Claude Desktop. hot_reflections accumulate organically,
cold_processor runs on schedule, evolution_queue surfaces improvements for owner approval.

---

## Active Tables
knowledge_base, mistakes, sessions, task_queue, changelog,
hot_reflections, cold_reflections, evolution_queue, pattern_frequency

---

## Step Status
- ✅ Step 0: Railway MCP Server + Telegram Bot - COMPLETE 2026-03-11
- ✅ Step 1: Claude Desktop Live Connection - COMPLETE 2026-03-11
- ✅ PRE-STEP 2: Fix t_state() - operating_context.json + SESSION.md fetch + sb_query param - DONE
- ✅ Step 2: Audit Training Logic - DONE 2026-03-11
- ✅ Step 3: Training Pipeline Implemented - DONE 2026-03-11 (commit fda0388)
- ✅ Step 4: Simulation - DONE 2026-03-11 (commit 66fa36b)
- ✅ Step 5: Deploy & Monitor - COMPLETE 2026-03-11

## Step 5 - Progress Log
- ✅ 2026-03-11i: processed_by_cold fix - eq.false→eq.0, eq.true→eq.1 in querystring (commit cc87e5c)
- ✅ 2026-03-11j: POST /patch endpoint added - surgical edits from claude.ai (commit cc87e5c)
- ✅ 2026-03-11k: Railway MCP server built & registered in claude_desktop_config.json
    7 tools: railway_status, railway_services, railway_env_get, railway_env_set,
             railway_logs, railway_restart, railway_deploy_status
    Location: C:\Users\rnvgg\.claude-skills\mcp-servers\railway-mcp\index.js
    NOTE: Requires Claude Desktop restart to activate
- ✅ MCP_SECRET added to CREDENTIALS.md (core_mcp_secret_2026_REINVAGNAR)
- ✅ Write efficiency rules documented in CORE_v5_plan.md
- ✅ DB cleanup: hot_reflections/cold_reflections/pattern_frequency sim+null rows deleted
- ✅ Railway health verified: all components OK (supabase, groq, telegram, github)
- ✅ Retroactive learnings injected from Steps 0-5:
    - 8 knowledge_base entries (architecture, training, bugs, workflow)
    - 6 mistakes (postgrest bool, sb_query param, rate limiter, egress, repo arg, t_state)
    - 4 hot_reflections (Step 0-1, PRE2+2, 3-4, 5) ready for cold_processor
    - 1 session log

---

## System State
- Railway: live @ https://core-agi-production.up.railway.app
- core.py: commit cc87e5c (processed_by_cold fix + /patch endpoint)
- MCP on CORE: 20 tools active
- knowledge_base: 340 entries (332 existing + 8 injected)
- hot_reflections: 7 rows (id 11-17), all unprocessed by cold → ready for next cold run
- Claude Desktop MCP servers: core-agi, railway, github, postgres, filesystem,
  memory, fetch, sqlite, windows-mcp, cloudflare-workers, cloudflare-builds,
  sequential-thinking, puppeteer, everything, zapier, git, time

---

## Surgical Edit Workflow (claude.ai)
claude.ai cannot reach Railway directly (egress blocked).
Use Desktop Commander PowerShell:
  $body = @{secret="core_mcp_secret_2026_REINVAGNAR"; path="core.py";
            old_str="..."; new_str="..."; message="fix: ..."} | ConvertTo-Json
  Invoke-RestMethod -Uri "https://core-agi-production.up.railway.app/patch" -Method POST
    -ContentType "application/json" -Body $body

---

## Rules for Claude Desktop Sessions
- NEVER pass `repo` arg ke read_file atau write_file
- NEVER gunakan `query_string` untuk sb_query - gunakan `filters`
- NEVER hardcode step numbers di core.py - pakai get_current_step()
- ALWAYS read-back setelah setiap write sebelum report success
- ALWAYS call get_mistakes(domain=X) sebelum remote write
- ALWAYS update SESSION.md di akhir session kalau ada yang berubah
- Edit file lokal: Desktop Commander:edit_block (surgical, bukan full rewrite)
- Edit GitHub file: gh_search_replace (Claude Desktop) atau /patch (claude.ai)
- File baru panjang: chunk 25-30 baris, jangan satu blob
- Kalau drop tabel baru: append ke tombstone_tables di operating_context.json

---

## Context Files
- `operating_context.json` - static: tool rules, schema, tombstone tables
- `SESSION.md` (file ini) - dynamic: active tables, step status, next action
- `TRAINING_DESIGN.md` - pipeline design lengkap (output Step 2)