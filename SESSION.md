# CORE v5.0 — Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: PRE-STEP 2 ✅ DONE → Next: Step 2

---

## Next Action
Step 2: Audit training logic — hot_reflections → cold_reflections → evolution_queue pipeline.

---

## Active Tables
*(Update bagian ini kalau ada perubahan skema Supabase)*

knowledge_base, mistakes, sessions, task_queue, changelog,
hot_reflections, cold_reflections, evolution_queue, pattern_frequency

---

## Step Status
- ✅ Step 0: Railway MCP Server + Telegram Bot
- ✅ Step 1: Claude Desktop Live Connection
- ✅ PRE-STEP 2: Fix t_state() — operating_context.json + SESSION.md fetch + sb_query param
- ⏳ Step 2: Audit Training Logic — hot_reflections → cold_reflections → evolution_queue
- ⏳ Step 3: Apply New Training Design
- ⏳ Step 4: Simulation
- ⏳ Step 5: Deploy & Monitor

---

## System State
- Railway: live at core-agi-production.up.railway.app
- MCP: Streamable HTTP, protocol 2024-11-05, 14 tools active
- Supabase: ok | Groq: ok | Telegram: ok | GitHub: ok
- Knowledge base: 330+ | Sessions: 180+ | Mistakes: 80+

---

## Rules for Claude Desktop Sessions
- NEVER pass `repo` arg ke read_file atau write_file
- NEVER gunakan `query_string` untuk sb_query — gunakan `filters`
- ALWAYS read-back setelah setiap write sebelum report success
- ALWAYS call get_mistakes(domain=X) sebelum remote write
- ALWAYS update SESSION.md di akhir session kalau ada yang berubah
- Kalau drop tabel baru: append ke `tombstone_tables` di operating_context.json
- Kalau tambah tabel baru: tambah ke Active Tables di file ini

---

## Context Files
- `operating_context.json` — static: tool rules, schema, tombstone tables (jarang berubah)
- `SESSION.md` (file ini) — dynamic: active tables, step status, next action (update tiap session)
