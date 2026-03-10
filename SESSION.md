# CORE v5.0 — Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: Step 4 — Simulation

---

## Next Action
Step 4: Simulate training loop end-to-end:
1. Insert test hot_reflections (with new_patterns)
2. Trigger cold processor via MCP
3. Verify pattern_frequency upsert
4. Verify evolution_queue entry (if frequency >= 3)
5. Test approve/reject flow via Telegram + MCP tool

---

## Active Tables
knowledge_base, mistakes, sessions, task_queue, changelog,
hot_reflections, cold_reflections, evolution_queue, pattern_frequency

---

## Step Status
- ✅ Step 0: Railway MCP Server + Telegram Bot
- ✅ Step 1: Claude Desktop Live Connection
- ✅ PRE-STEP 2: Fix t_state() — operating_context.json + SESSION.md fetch + sb_query param
- ✅ Step 2: Audit Training Logic — DONE 2026-03-11
- ✅ Step 3: Training Pipeline Implemented — DONE 2026-03-11 (commit fda0388)
- 🔄 Step 4: Simulation — IN PROGRESS
- ⏳ Step 5: Deploy & Monitor

## Step 3 — What was built
- `auto_hot_reflection()` — auto-write hot_reflections on every session insert
- `run_cold_processor()` — batch: distill patterns, upsert pattern_frequency, queue evolutions
- `cold_processor_loop()` — background thread, check every 30min (10 hot OR 24h)
- `apply_evolution()` — apply approved evolutions (knowledge/code/behavior)
- `reject_evolution()` — reject + log as mistake
- MCP tools: `list_evolutions`, `trigger_cold_processor`, `approve_evolution`, `reject_evolution` (17 total)
- Telegram: `/evolutions`, `/approve <id>`, `/reject <id>`

---

## System State
- Railway: Step 3 live (commit fda0388)
- MCP: 17 tools active
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
- `operating_context.json` — static: tool rules, schema, tombstone tables
- `SESSION.md` (file ini) — dynamic: active tables, step status, next action
- `TRAINING_DESIGN.md` — pipeline design lengkap (output Step 2)
