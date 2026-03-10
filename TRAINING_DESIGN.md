# CORE v5.0 — Training Pipeline Design
**Status:** FINAL — Step 2 output
**Designed:** 2026-03-11
**Owner:** REINVAGNAR

---

## Philosophy

Sistem belajar dari dirinya sendiri melalui loop:
```
Session selesai → hot_reflections → cold processor → pattern_frequency → evolution_queue → apply → loop
```
Schema tidak perlu diubah — sudah matang dan siap diimplementasikan.

---

## Pipeline Full

### Stage 1 — HOT REFLECTION (per session)

**Trigger:** Session selesai

**Dua cara tulis ke `hot_reflections`:**

1. **Claude manual (via MCP `sb_insert`)** — di akhir setiap Claude Desktop session
   - Cocok untuk refleksi yang butuh reasoning: gaps, patterns, kualitas
   - Fields yang diisi Claude: `task_summary`, `domain`, `quality_score`, `new_patterns[]`, `new_mistakes[]`, `gaps_identified`, `reflection_text`

2. **core.py otomatis** — triggered saat row baru masuk ke tabel `sessions`
   - Auto-generate dari data yang sudah ada: `summary` → `task_summary`, parse `actions[]` untuk hitung `verify_rate` & `mistake_consult_rate`
   - Lightweight, tidak butuh reasoning
   - Fields yang diisi: `task_summary`, `domain`, `verify_rate`, `mistake_consult_rate`

**Default:** `processed_by_cold = false`

---

### Stage 2 — COLD PROCESSOR (batch)

**Trigger (mana yang duluan):**
- 10 hot_reflections belum diproses (`processed_by_cold=false`), ATAU
- 24 jam sejak cold processor terakhir jalan

**Dijalankan oleh dua cara:**

1. **core.py background thread** — check setiap 30 menit, auto-trigger kalau kondisi terpenuhi
2. **Claude manual via MCP** — Claude bisa trigger kapan saja via tool call ke endpoint khusus

**Proses:**
```
1. Baca semua hot_reflections WHERE processed_by_cold=false
2. Untuk setiap pattern di new_patterns[]:
   - Kalau pattern_key sudah ada di pattern_frequency → increment frequency, update last_seen
   - Kalau baru → insert baru (frequency=1, confidence=0.5)
3. Kalau frequency >= 3 → push ke evolution_queue
4. Tulis 1 baris cold_reflections:
   - period_start/end, hot_count, patterns_found, evolutions_queued, summary_text
5. Update semua hot yang diproses: processed_by_cold=true
```

---

### Stage 3 — EVOLUTION QUEUE (apply perubahan)

**Tiga tipe perubahan, tiga level keamanan:**

| change_type | Auto-apply? | Kondisi | Target |
|---|---|---|---|
| `knowledge` | ✅ Ya | confidence > 0.7 | knowledge_base via add_knowledge |
| `code` | ❌ Tidak | SELALU butuh owner approval | core.py via GitHub push |
| `behavior` | ❌ Tidak | SELALU butuh owner approval | operating_context.json atau SESSION.md |

**Flow:**
```
evolution_queue (status=pending)
  → notify_owner via Telegram (owner_notified=true)
  
  IF change_type=knowledge AND confidence > 0.7:
    → auto-apply: add_knowledge MCP call
    → status=applied, applied_at=now
  
  IF change_type=code OR behavior:
    → tunggu owner approval via Telegram
    → owner approve → apply diff_content
    → status=applied, applied_at=now
  
  IF owner reject:
    → status=rejected
    → log ke mistakes (domain=evolution)
```

**Rollback:** `reversible=true` default — owner bisa revert via Telegram command kapan saja.

---

## Decisions Made

| Decision | Nilai | Alasan |
|---|---|---|
| Cold processor trigger | 10 hot ATAU 24 jam | Responsif tapi tidak wasteful |
| Pattern threshold | frequency >= 3 | Satu atau dua kali bisa noise, tiga kali = signal |
| Knowledge auto-apply | confidence > 0.7 | Risiko rendah, tidak perlu selalu gangguin owner |
| Code/behavior | Selalu manual | Risiko tinggi, butuh human judgment |
| Rollback default | reversible=true | Sesuai constitution: always recoverable |

---

## Schema Migration

**Tidak ada perubahan schema yang diperlukan.** Semua field yang dibutuhkan pipeline sudah ada.

Satu hal yang perlu ditambahkan saat implementasi: **index** di:
- `hot_reflections.processed_by_cold` — untuk query WHERE processed_by_cold=false yang efisien
- `pattern_frequency.pattern_key` — untuk upsert yang cepat
- `evolution_queue.status` — untuk filter pending

---

## Implementation Notes untuk Step 3

### Fungsi baru yang perlu dibuat di core.py:

```python
# 1. Auto-write hot reflection saat session di-insert
def auto_hot_reflection(session_data): ...

# 2. Cold processor — bisa dipanggil oleh thread ATAU MCP tool
def run_cold_processor(): ...

# 3. Evolution applier — dipanggil setelah owner approve
def apply_evolution(evolution_id): ...

# 4. Telegram command handler baru
# /approve <id> — approve evolution
# /reject <id>  — reject evolution
# /evolutions   — list pending evolutions
```

### MCP tool baru yang perlu diekspos:
```
trigger_cold_processor  — Claude bisa trigger manual
list_evolutions         — lihat pending evolutions
approve_evolution       — approve dari Claude Desktop
```

### Background thread baru di core.py:
```python
def cold_processor_loop():
    # Check setiap 30 menit
    # Trigger kalau hot_count >= 10 ATAU 24 jam berlalu
    while True:
        run_cold_processor()
        time.sleep(1800)  # 30 menit
```

---

## Free Tier Considerations

- Cold processor jalan **maksimal 2x per hari** (24 jam trigger) → ~60 Supabase reads/writes per hari, aman
- Evolution knowledge auto-apply → 1 Supabase write per evolution, aman
- Groq **tidak dipakai** di training pipeline ini — cold processor logic murni Python/SQL
- Railway background thread ringan — tidak ada LLM call, hanya DB queries
