# CORE v5.0 — Session State

**Last updated:** 2026-03-11
**Owner:** REINVAGNAR

## Current Step: Step 2 IN PROGRESS

---

## Next Action
Step 2.2: Design training pipeline — trigger, flow, produce/consume logic.

---

## Step 2 Progress
- ✅ 2.1: Schema audit — semua 4 tabel confirmed, lihat TRAINING_DESIGN.md
- ⏳ 2.2: Design pipeline (trigger, flow, produce/consume)
- ⏳ 2.3: Keputusan schema migration (kalau perlu)
- ⏳ 2.4: Dokumentasi final → TRAINING_DESIGN.md

### Schema Summary (verified 2026-03-11)
**hot_reflections**: id, created_at, task_summary, domain, verify_rate, mistake_consult_rate, new_patterns[], new_mistakes[], quality_score, gaps_identified, reflection_text, processed_by_cold(bool)

**cold_reflections**: id, created_at, period_start, period_end, hot_count, patterns_found, evolutions_queued, auto_applied, summary_text

**evolution_queue**: id, created_at, status(pending), confidence, impact, reversible(bool), change_type, change_summary, diff_content, pattern_key, frequency, owner_notified(bool), applied_at

**pattern_frequency**: id, pattern_key, domain, description, frequency(int), confidence(float), first_seen, last_seen, auto_applied(bool)

### Note
- Semua 4 tabel kosong (belum pernah dipakai)
- Schema sudah matang — pipeline tinggal diimplementasikan
- Dummy rows id=1 di tiap tabel (dari schema probe) — bisa diabaikan

---

## Active Tables
knowledge_base, mistakes, sessions, task_queue, changelog,
hot_reflections, cold_reflections, evolution_queue, pattern_frequency

---

## Step Status
- ✅ Step 0: Railway MCP Server + Telegram Bot
- ✅ Step 1: Claude Desktop Live Connection
- ✅ PRE-STEP 2: Fix t_state() — operating_context.json + SESSION.md fetch + sb_query param
- 🔄 Step 2: Audit Training Logic — IN PROGRESS (2.1 done)
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
- `TRAINING_DESIGN.md` — output Step 2: pipeline design lengkap (dibuat Step 2.4)
