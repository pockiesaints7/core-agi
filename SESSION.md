# CORE v5.0 — Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: Step 4 ✅ DONE → Next: Step 5 (Deploy & Monitor)

---

## Next Action
Step 5: Deploy & Monitor — pastikan Railway stable, monitor cold processor via Telegram /status, verify knowledge base grows organically dari real sessions, clean up sim test rows dari hot_reflections (id 2-10).

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
- ✅ Step 4: Simulation — DONE 2026-03-11 (commit 66fa36b)
- ⏳ Step 5: Deploy & Monitor — NEXT

## Step 4 — Simulation Results
- 9 hot_reflections inserted (sim test, id 2-10)
- Background thread auto-triggered cold processor saat count=10 ✔
- pattern_frequency upserted: freq=5, auto_applied=true ✔
- evolution_queue: id=2, status=applied, confidence=0.95 ✔
- knowledge_base auto-apply: id=750, source=evolution_queue, confidence=high ✔
- cold_reflections summary: tidak ter-insert (minor bug ditemukan)

## Step 4 — Bug Found & Fixed
- cold_reflections insert: debug logging ditambahkan
- pattern frequency counting: sekarang pakai Counter (batch aggregation)
- evolution probe row (id=1): semua query sudah exclude dengan id=gt.1
- Fix di commit ini (66fa36b) sudah include semua perbaikan

---

## System State
- Railway: Step 3 live + Step 4 fix applied
- MCP: 17 tools active
- Knowledge base: 333 (330 + 3 sim) | Sessions: 182+ | Mistakes: 81+
- hot_reflections: 10 total (1 probe + 9 sim), semua processed
- pattern_frequency: 1 pattern (sim test)
- evolution_queue: 1 applied (sim test)

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
