# CORE v5.0 — Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: Step 2 ✅ DONE → Next: Step 3

---

## Next Action
Step 3: Implementasi training pipeline di core.py — auto_hot_reflection, cold_processor_loop, evolution applier, MCP tools baru, Telegram commands baru.

---

## Step 2 Progress
- ✅ 2.1: Schema audit — semua 4 tabel confirmed
- ✅ 2.2: Pipeline design — trigger, flow, produce/consume
- ✅ 2.3: Schema migration — tidak diperlukan, schema sudah matang
- ✅ 2.4: TRAINING_DESIGN.md dibuat di repo

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
- ⏳ Step 3: Apply New Training Design — NEXT
- ⏳ Step 4: Simulation
- ⏳ Step 5: Deploy & Monitor

---

## Step 3 Scope (dari TRAINING_DESIGN.md)
Fungsi baru di core.py:
- `auto_hot_reflection()` — auto-write saat session di-insert
- `run_cold_processor()` — batch processor, dipanggil thread ATAU MCP
- `apply_evolution()` — apply setelah owner approve
- `cold_processor_loop()` — background thread, check tiap 30 menit

MCP tools baru:
- `trigger_cold_processor`, `list_evolutions`, `approve_evolution`

Telegram commands baru:
- `/approve <id>`, `/reject <id>`, `/evolutions`

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
- `operating_context.json` — static: tool rules, schema, tombstone tables
- `SESSION.md` (file ini) — dynamic: active tables, step status, next action
- `TRAINING_DESIGN.md` — pipeline design lengkap (output Step 2)
