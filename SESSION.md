# CORE v5.0 - Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: ✅ ALL STEPS COMPLETE — CORE v5.0 LIVE

---

## Next Action
CORE v5.0 fully live. Training loop confirmed working.
CORE_SELF.md created — living self-knowledge document. CORE now knows itself permanently.
Schema evolution protocol in place — any structural change triggers update to CORE_SELF.md + operating_context.json + KB.
Next: use CORE for real tasks. Feed hot_reflections naturally per session.

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
- ✅ Step 6: CORE Self-Knowledge — COMPLETE 2026-03-11

---

## System State (as of 2026-03-11 session)
- Railway: live @ https://core-agi-production.up.railway.app
- core.py: commit 19b0d0f (restored + self_sync_check added)
- last_good_commit: 19b0d0f ← UPDATE THIS after every successful deploy
- knowledge_base: ~360+ entries
- CORE_SELF.md: LIVE — single source of truth for CORE's self-knowledge
- operating_context.json: v2.0 — full schema all 9 active tables + architecture
- MCP on CORE: 20 tools active
- resource_ceilings.json: v2 — includes _env_vars_declared + _ai_models + _last_verified

## Incident Log
- 2026-03-11 INCIDENT: write_file tool wiped core.py (929→26 lines). Railway down ~5min.
  Root cause: write_file = full overwrite, used for partial docstring edit.
  Recovery: github:get_file_contents from commit cc87e5c → restore via github:create_or_update_file.
  Fixes applied: self_sync_check() added, write_file desc updated in TOOLS registry.
  KB entries added: "CORE Critical Rule — write_file is FULL OVERWRITE" + recovery procedure.

---

## Self-Knowledge Files (in order of authority)
1. `CORE_SELF.md` — Master. Update FIRST on any structural change.
2. `operating_context.json` — Static tool rules + schema (sync from CORE_SELF.md)
3. KB domain=system — Supabase searchable copy (sync from CORE_SELF.md)
4. `SESSION.md` (this file) — Dynamic per-session state only

---

## Schema Evolution Protocol (summary)
When ANYTHING structural changes → update in this order:
1. CORE_SELF.md (GitHub) ← always first
2. operating_context.json (GitHub)
3. KB entry for that component (Supabase)
4. SESSION.md if active tables changed
5. changelog row (Supabase)
Full protocol: see CORE_SELF.md → "Schema Evolution Protocol" section

---

## Injected SOPs (searchable in knowledge_base domain=workflow)
- SOP: Tool Routing — Read Operations
- SOP: Tool Routing — Write Operations
- SOP: Tool Routing — HTTP / External Calls
- SOP: Session Start Checklist
- SOP: Rate Limit Management
- SOP: When to Use Claude Desktop vs claude.ai
- SOP: CORE Routing Engine — Task Archetype Classification (A1-A7)
- SOP: CORE Routing Engine — 5-Layer Decision Logic
- SOP: CORE Routing Engine — Per-Archetype Tool Rules
- SOP: CORE Routing Engine — Domain-Specific Routing Patterns
- SOP: CORE Routing Engine — Top 10 Efficiency Insights
- SOP: CORE Routing Engine — Evolved Rules from Edge-Case Simulation
- SOP: CORE Routing Engine — Safety-Critical Domain Rules

---

## Surgical Edit Workflow (claude.ai)
Use Desktop Commander PowerShell:
  $body = @{secret="core_mcp_secret_2026_REINVAGNAR"; path="core.py"; old_str="..."; new_str="..."; message="fix: ..."} | ConvertTo-Json
  Invoke-RestMethod -Uri "https://core-agi-production.up.railway.app/patch" -Method POST -ContentType "application/json" -Body $body

---

## Rules for All Sessions
- NEVER pass `repo` arg ke read_file atau write_file
- NEVER gunakan `query_string` untuk sb_query - gunakan `filters`
- NEVER hardcode step numbers di core.py
- ALWAYS read-back setelah setiap write
- ALWAYS call get_mistakes(domain=X) sebelum remote write
- ALWAYS update SESSION.md di akhir session kalau ada yang berubah
- ALWAYS update CORE_SELF.md FIRST kalau ada structural change
- Edit GitHub file: gh_search_replace (Claude Desktop) atau /patch (claude.ai)

---

## Context Files
- `CORE_SELF.md` — MASTER: full self-knowledge, schemas, evolution protocol
- `operating_context.json` — static: tool rules, schema (synced from CORE_SELF.md)
- `SESSION.md` (this file) — dynamic: active tables, step status, next action
- `TRAINING_DESIGN.md` — pipeline design reference
