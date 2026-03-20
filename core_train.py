"""core_train.py — CORE AGI Training Pipeline
Extracted from core.py. Contains:
  - auto_hot_reflection
  - run_cold_processor
  - apply_evolution / reject_evolution
  - cold_processor_loop
  - _backlog_add / _sync_backlog_status / _backlog_to_markdown
  - run_kb_mining
  - _extract_real_signal / _run_simulation_batch
  - background_researcher

Depends on: core_config, core_github
NOTE: Split architecture is live. core.py has been deleted.
"""
import json
import os
import time
import threading as _threading
from collections import Counter
from datetime import datetime, timedelta

import httpx

from core_config import (
    SUPABASE_URL,
    COLD_HOT_THRESHOLD, COLD_TIME_THRESHOLD, COLD_KB_GROWTH_THRESHOLD,
    PATTERN_EVO_THRESHOLD, KNOWLEDGE_AUTO_CONFIDENCE,
    KB_MINE_BATCH_SIZE, KB_MINE_RATIO_THRESHOLD,
    sb_get, sb_post, sb_post_critical, sb_patch, sb_upsert, _sbh_count_svc,
    gemini_chat, groq_chat, GROQ_MODEL,
)
from core_github import notify, gh_write

# Training globals
_last_cold_run: float = 0.0
_last_cold_kb_count: int = 0
_last_research_run: float = -1.0
_IMPROVEMENT_INTERVAL = 3600  # 60 min
_last_public_source_run: float = -1.0
_PUBLIC_SOURCE_INTERVAL = 21600  # 6 hours

# Source confidence multipliers (Phase 3)
_SRC_CONF = {"real": 1.0, "simulation": 0.7, "both": 1.3}

# AGI-02: Nightly self-diagnosis
# NOTE: _last_self_diagnosis_run is intentionally NOT initialised to 0.0 here.
# It is seeded from Supabase at cold_processor_loop boot to survive Railway redeploys.
# Initialised to a sentinel so the boot-restore logic can detect first-run.
_last_self_diagnosis_run: float = -1.0   # -1 = not yet seeded from Supabase
_SELF_DIAGNOSIS_INTERVAL = 86400  # 24 hours
_SELF_DIAGNOSIS_HOUR_UTC = 19     # 02:00 WIB = 19:00 UTC
_DIAG_STATE_KEY = "last_self_diagnosis_ts"  # key used in sessions state_key

# AGI-01: Weekly cross-domain synthesis
_last_synthesis_run: float = 0.0
_SYNTHESIS_INTERVAL = 6 * 24 * 3600  # 6 days
_SYNTHESIS_DAY_UTC = 2               # Wednesday UTC

# AGI-05: Weekly memory consolidation
_last_consolidation_run: float = 0.0
_CONSOLIDATION_INTERVAL = 6 * 24 * 3600  # 6 days
_CONSOLIDATION_DAY_UTC = 6               # Sunday UTC (weekday=6)

# AGI-06: Weekly capability report
_last_capability_report_run: float = 0.0
_CAPABILITY_REPORT_INTERVAL = 6 * 24 * 3600  # 6 days
_CAPABILITY_REPORT_DAY_UTC = 1               # Tuesday UTC (weekday=1)

# ── TASK-4: Binance Price Monitor config ──────────────────────────────────────
_PRICE_MONITOR_SYMBOLS  = os.getenv("BINANCE_WATCH_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",")
_PRICE_ALERT_THRESHOLD  = float(os.getenv("BINANCE_ALERT_THRESHOLD_PCT", "3.0"))
_PRICE_MONITOR_INTERVAL = int(os.getenv("BINANCE_MONITOR_INTERVAL_S", "60"))
_price_monitor_last_prices: dict = {}
_price_monitor_running = False


# -- Helpers (imported by core_main for get_system_counts) --------------------
def get_system_counts():
    counts = {}
    table_filters = {
        "knowledge_base":  "",
        "mistakes":        "",
        "sessions":        "",
        "task_queue":      "",
        "hot_reflections": "&processed_by_cold=eq.0",
        "evolution_queue": "&status=eq.pending",
    }
    for t, extra in table_filters.items():
        try:
            r = httpx.get(f"{SUPABASE_URL}/rest/v1/{t}?select=id&limit=1{extra}",
                          headers=_sbh_count_svc(), timeout=10)
            cr = r.headers.get("content-range", "*/0")
            counts[t] = int(cr.split("/")[-1]) if "/" in cr else 0
        except:
            counts[t] = -1
    return counts


def get_latest_session():
    d = sb_get("sessions", "select=summary,actions,created_at&order=created_at.desc&limit=1")
    return d[0] if d else {}


# -- Hot reflection ------------------------------------------------------------
def auto_hot_reflection(session_data: dict):
    try:
        summary       = session_data.get("summary", "")
        actions       = session_data.get("actions", []) or []
        interface     = session_data.get("interface", "unknown")
        seed_patterns = session_data.get("seed_patterns", []) or []
        total     = max(len(actions), 1)
        verify_rate  = round(sum(1 for a in actions if any(k in str(a).lower() for k in ["verify","readback","confirm"])) / total, 2)
        mistake_rate = round(sum(1 for a in actions if any(k in str(a).lower() for k in ["mistake","error","fix","wrong"])) / total, 2)
        domain = "general"
        _summary_lower = summary.lower()
        _domain_map = [
            (["supabase", "sb_query", "sb_patch", "sb_insert", "database", "table"], "db"),
            (["github", "patch_file", "gh_search", "gh_read", "commit", "deploy", "railway"], "code"),
            (["telegram", "notify", "bot"], "bot"),
            (["mcp", "tool", "tools dict", "session_start", "session_end"], "mcp"),
            (["training", "cold processor", "hot_reflection", "evolution", "pattern"], "training"),
            (["knowledge", "kb", "add_knowledge", "search_kb"], "kb"),
            (["architecture", "refactor", "skill file", "session_md", "system_map"], "core_agi.architecture"),
            (["patching", "patch", "old_str", "new_str"], "core_agi.patching"),
        ]
        for keywords, d in _domain_map:
            if any(kw in _summary_lower for kw in keywords):
                domain = d
                break
        if len(summary.strip()) < 50 and total <= 2:
            print(f"[HOT] Skipped trivial session: summary_len={len(summary)} actions={total}")
            return False

        # Extract patterns via Groq so cold processor has real signal
        new_patterns = list(seed_patterns)  # start with caller-supplied patterns
        quality_score = session_data.get("quality") or None
        gaps_identified = None
        try:
            actions_str = ", ".join(str(a) for a in actions[:20])
            seed_hint = f"Caller already identified: {seed_patterns}\n" if seed_patterns else ""

            anchor_ts = None
            try:
                prev = sb_get("hot_reflections",
                    "select=created_at&order=created_at.desc&limit=1",
                    svc=True)
                if prev and prev[0].get("created_at"):
                    anchor_ts = prev[0]["created_at"]
            except Exception:
                pass
            if not anchor_ts:
                anchor_ts = session_data.get("created_at") or ""
            if not anchor_ts:
                anchor_ts = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            session_ts = anchor_ts.replace("Z", "").split("+")[0]

            enrichment = ""
            try:
                new_mistakes = sb_get("mistakes",
                    f"select=domain,what_failed,root_cause&created_at=gte.{session_ts}&order=id.desc&limit=5",
                    svc=True)
                if new_mistakes:
                    enrichment += "\nNew mistakes this session:\n" + "\n".join(
                        f"  [{r.get('domain','?')}] {r.get('what_failed','')[:120]} | root: {r.get('root_cause','')[:80]}"
                        for r in new_mistakes)
            except Exception: pass
            try:
                new_kb = sb_get("knowledge_base",
                    f"select=domain,topic&updated_at=gte.{session_ts}&order=updated_at.desc&limit=5",
                    svc=True)
                if new_kb:
                    enrichment += "\nKB entries added/updated:\n" + "\n".join(
                        f"  [{r.get('domain','?')}] {r.get('topic','')[:100]}" for r in new_kb)
            except Exception: pass
            try:
                task_updates = sb_get("task_queue",
                    f"select=task,status,result&updated_at=gte.{session_ts}&order=updated_at.desc&limit=5",
                    svc=True)
                if task_updates:
                    enrichment += "\nTask queue updates:\n" + "\n".join(
                        f"  [{r.get('status','?')}] {str(r.get('task',''))[:100]} | result: {str(r.get('result','null'))[:60]}"
                        for r in task_updates)
            except Exception: pass
            try:
                changelog_rows = sb_get("changelog",
                    f"select=component,title,change_type&created_at=gte.{session_ts}&order=id.desc&limit=3",
                    svc=True)
                if changelog_rows:
                    enrichment += "\nChangelog entries:\n" + "\n".join(
                        f"  [{r.get('change_type','?')}] {r.get('component','?')}: {r.get('title','')[:100]}"
                        for r in changelog_rows)
            except Exception: pass

            prompt = (
                f"Session summary: {summary[:500]}\n"
                f"Actions taken: {actions_str}\n"
                f"{enrichment}\n"
                f"{seed_hint}\n"
                f"Extract 2-5 reusable patterns from this session. "
                f"Each pattern should be a short, generalizable rule or observation (under 120 chars). "
                f"Do NOT duplicate patterns already listed above. "
                f"Also rate session quality 0.0-1.0 and identify any gaps.\n"
                f"Respond ONLY as JSON: "
                f'{{"patterns": ["..."], "quality": 0.8, "gaps": "..or null"}}'
            )
            raw = gemini_chat(system="You are CORE's pattern extraction engine. Return only valid JSON.", user=prompt, max_tokens=500, json_mode=True)
            raw_clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            parsed = json.loads(raw_clean)
            groq_patterns = [p for p in parsed.get("patterns", []) if isinstance(p, str) and len(p) > 5][:5]
            seen = set(p.lower() for p in new_patterns)
            for p in groq_patterns:
                if p.lower() not in seen:
                    new_patterns.append(p)
                    seen.add(p.lower())
            if quality_score is None:
                quality_score = float(parsed.get("quality") or 0.7)
            _gaps_raw = parsed.get("gaps") or None
            gaps_identified = [_gaps_raw] if _gaps_raw and isinstance(_gaps_raw, str) else _gaps_raw
            print(f"[HOT] Groq extracted {len(groq_patterns)} patterns, merged total={len(new_patterns)}, quality={quality_score}")
        except Exception as e:
            print(f"[HOT] Pattern extraction failed (non-fatal): {e}")

        ok = sb_post("hot_reflections", {
            "task_summary": summary[:300], "domain": domain,
            "verify_rate": verify_rate, "mistake_consult_rate": mistake_rate,
            "new_patterns": new_patterns, "new_mistakes": [],
            "quality_score": quality_score, "gaps_identified": gaps_identified,
            "reflection_text": f"Auto-generated from {interface} session. Actions: {total}. Patterns: {len(new_patterns)}.",
            "processed_by_cold": 0,
        })
        print(f"[HOT] ok={ok} domain={domain}")
        return ok
    except Exception as e:
        print(f"[HOT] error: {e}")
        return False


# -- Knowledge ingestion bridge -----------------------------------------------
def _ingest_to_hot_reflection(topic: str, source_type: str, concept_clusters: list, engagement_avg: float) -> bool:
    if not concept_clusters:
        print(f"[INGEST->HOT] No concepts found for topic={topic}, skipping")
        return False

    inserted = 0
    for concept in concept_clusters:
        try:
            quality_score = round(min(1.0, engagement_avg / 100.0), 3)
            new_patterns = []
            gaps_identified = None
            try:
                prompt = (
                    f"Topic: {topic}\n"
                    f"Concept: {concept}\n"
                    f"Sources: {source_type}\n"
                    f"Avg community engagement score: {engagement_avg:.1f}/100\n\n"
                    f"Extract 2-4 reusable patterns about '{concept}' that CORE should internalize. "
                    f"Each pattern = short actionable rule (<120 chars). "
                    f"Also identify 1 gap or open question about this concept.\n"
                    f"Respond ONLY as JSON: "
                    f'{{"patterns": ["..."], "gap": "..or null"}}'
                )
                raw = groq_chat(
                    system="You are CORE's knowledge synthesis engine. Return only valid JSON.",
                    user=prompt, model=GROQ_FAST, max_tokens=400,
                )
                parsed = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
                new_patterns = [p for p in parsed.get("patterns", []) if isinstance(p, str) and len(p) > 5][:4]
                gap_raw = parsed.get("gap") or None
                gaps_identified = [gap_raw] if gap_raw and isinstance(gap_raw, str) else None
            except Exception as e:
                print(f"[INGEST->HOT] Groq synthesis failed for {concept} (non-fatal): {e}")
                new_patterns = [f"Community knowledge on {concept}: avg engagement {engagement_avg:.0f}/100"]

            ok = sb_post("hot_reflections", {
                "task_summary":          f"Knowledge ingest: {topic} — concept: {concept}",
                "domain":                "knowledge_ingestion",
                "verify_rate":           0,
                "mistake_consult_rate":  0,
                "new_patterns":          new_patterns,
                "new_mistakes":          [],
                "quality_score":         quality_score,
                "gaps_identified":       gaps_identified,
                "reflection_text":       f"Ingested from {source_type}. Topic: {topic}. Concept: {concept}. Avg engagement: {engagement_avg:.1f}/100.",
                "processed_by_cold":     0,
                "source":                "real",
            })
            if ok:
                inserted += 1
        except Exception as e:
            print(f"[INGEST->HOT] Error for concept={concept}: {e}")
            continue

    print(f"[INGEST->HOT] Done: {inserted}/{len(concept_clusters)} hot_reflections inserted")
    return inserted > 0


# -- Gaps reconciliation -------------------------------------------------------
def _reconcile_gaps(hots: list) -> int:
    try:
        raw_gaps = []
        for h in hots:
            gaps = h.get("gaps_identified") or []
            if isinstance(gaps, str):
                try:
                    gaps = json.loads(gaps)
                except Exception:
                    gaps = [gaps]
            domain = h.get("domain", "general")
            for g in gaps:
                if g and isinstance(g, str) and len(g) > 10:
                    raw_gaps.append({"gap": g.strip()[:400], "domain": domain})

        if not raw_gaps:
            return 0

        gaps_text = "\n".join(f"  [{i+1}] [{g['domain']}] {g['gap']}" for i, g in enumerate(raw_gaps))
        prompt = (
            f"You are CORE's gap reconciliation engine.\n"
            f"Below are {len(raw_gaps)} gaps identified from recent hot reflections.\n\n"
            f"{gaps_text}\n\n"
            f"Respond with ONLY a JSON array of unique, actionable gaps (no duplicates, no vague items).\n"
            f"Each item: {{\"gap\": \"concise gap description\", \"domain\": \"domain\", \"priority\": 1-5}}\n"
            f"Priority 5=critical architecture gap, 1=minor improvement. Max 10 items. No preamble."
        )
        deduped = None
        try:
            raw = gemini_chat(
                system="You are CORE's gap reconciliation engine. Respond only with valid JSON array. No preamble.",
                user=prompt, max_tokens=600, json_mode=True,
            )
            parsed = json.loads(raw.strip())
            if isinstance(parsed, list):
                deduped = parsed
        except Exception as groq_err:
            print(f"[COLD] _reconcile_gaps Groq fallback (non-fatal): {groq_err}")
        if not deduped:
            seen = set()
            deduped = []
            for g in raw_gaps:
                if g["gap"] not in seen:
                    seen.add(g["gap"])
                    deduped.append({"gap": g["gap"], "domain": g["domain"], "priority": 2})
            deduped = deduped[:10]

        inserted = 0
        for item in deduped:
            gap_text = item.get("gap", "")[:400]
            domain = item.get("domain", "general")
            priority = int(item.get("priority", 2))
            if not gap_text:
                continue
            existing = sb_get("evolution_queue",
                f"select=id&pattern_key=ilike.%25{gap_text[:100].replace(' ', '%25')}%25&status=eq.pending&limit=1",
                svc=True)
            if existing:
                print(f"[COLD] Skipped duplicate gap (pending): {gap_text[:80]}")
                continue
            confidence = round(min(0.3 + priority * 0.1, 0.8), 2)
            ok = sb_post_critical("evolution_queue", {
                "change_type":    "code",
                "change_summary": f"[GAP] {gap_text}",
                "pattern_key":    f"gap:{gap_text[:200]}",
                "confidence":     confidence,
                "status":         "pending",
                "source":         "real",
                "impact":         domain,
                "recommendation": f"Gap identified in hot_reflections (priority={priority}). Requires architectural review.",
            })
            if ok:
                inserted += 1

        print(f"[COLD] _reconcile_gaps: raw={len(raw_gaps)} deduped={len(deduped)} inserted={inserted}")
        return inserted
    except Exception as e:
        print(f"[COLD] _reconcile_gaps error (non-fatal): {e}")
        return 0


# -- Cold processor ------------------------------------------------------------
def _groq_synthesize_cold(hots: list, batch_counts: Counter, batch_domain: dict) -> str:
    fallback = f"Processed {len(hots)} hots. {len(batch_counts)} unique patterns."
    try:
        top = sorted(batch_counts.items(), key=lambda x: x[1], reverse=True)[:15]
        top_text = "\n".join(
            f"  [{batch_domain.get(k,'?')}] ({v}x) {k[:120]}"
            for k, v in top
        )
        domain_counts: Counter = Counter(batch_domain[k] for k in batch_counts)
        domain_text = ", ".join(f"{d}:{n}" for d, n in domain_counts.most_common(6))
        session_summaries = "\n".join(
            f"  - {h.get('task_summary','')[:150]}" for h in hots[:10]
        )
        prompt = (
            f"You are CORE's cold processor synthesis engine.\n"
            f"You just processed {len(hots)} hot reflections covering {len(batch_counts)} unique patterns.\n\n"
            f"Domain breakdown: {domain_text}\n\n"
            f"Session summaries:\n{session_summaries}\n\n"
            f"Top patterns by frequency:\n{top_text}\n\n"
            f"Respond with ONLY a JSON object matching this exact schema (no preamble, no markdown):\n"
            f"{{\n"
            f"  \"themes\": \"2-3 sentence summary of dominant themes and domains\",\n"
            f"  \"top_patterns\": [\"pattern 1\", \"pattern 2\", \"pattern 3\"],\n"
            f"  \"gaps\": [\"gap or risk 1\", \"gap or risk 2\"],\n"
            f"  \"health_signal\": \"one sentence system health assessment\",\n"
            f"  \"summary\": \"3-5 sentence plain text synthesis combining all above\"\n"
            f"}}\n"
        )
        raw = gemini_chat(
            system="You are CORE's cold processor synthesis engine. Respond only with valid JSON. No preamble.",
            user=prompt, max_tokens=500, json_mode=True,
        )
        try:
            parsed = json.loads(raw.strip())
            synthesis = parsed.get("summary", "").strip()
            if not synthesis:
                synthesis = parsed.get("themes", "").strip()
            if len(synthesis) > 50:
                return synthesis
            parts = [parsed.get("themes", ""), " | ".join(parsed.get("top_patterns", [])[:3])]
            synthesis = " ".join(p for p in parts if p)
            return synthesis if len(synthesis) > 20 else fallback
        except (json.JSONDecodeError, ValueError, AttributeError):
            print(f"[COLD] synthesis JSON parse failed, using raw text fallback")
            synthesis = raw.strip()
            if len(synthesis) > 50:
                return synthesis
            return fallback
    except Exception as e:
        print(f"[COLD] synthesis failed (non-fatal): {e}")
        return fallback


def _groq_kb_content(pattern_key: str, domain: str, frequency: int, src_key: str) -> str:
    try:
        prompt = (
            f"You are CORE's knowledge base writer.\n"
            f"A pattern has recurred {frequency}x across real sessions (source: {src_key}).\n"
            f"Domain: {domain}\n"
            f"Pattern: {pattern_key}\n\n"
            f"Write a concise KB entry for this pattern. Include:\n"
            f"1. The rule stated clearly (1-2 sentences)\n"
            f"2. Why it matters / what failure it prevents\n"
            f"3. How to apply it (concrete action)\n"
            f"4. Any known exceptions\n"
            f"Max 200 words. No markdown headers. Plain paragraphs only."
        )
        content = gemini_chat(system="You are CORE's knowledge synthesis engine. Write clear actionable KB content.", user=prompt, max_tokens=350)
        content = content.strip()
        if len(content) > 30:
            return content
        return pattern_key
    except Exception as e:
        print(f"[COLD] kb_content generation failed (non-fatal): {e}")
        return pattern_key


def _backfill_patterns(batch_size: int = 10) -> int:
    try:
        rows = sb_get(
            "pattern_frequency",
            "select=id,pattern_key,frequency,domain&frequency=gte.2&auto_applied=eq.false&stale=eq.false&order=frequency.desc",
            svc=True
        ) or []
        if not rows:
            print("[BACKFILL] No patterns to backfill.")
            return 0

        rows = rows[:batch_size]
        inserted = 0

        for row in rows:
            key    = row.get("pattern_key", "")[:500]
            freq   = row.get("frequency", 2)
            domain = row.get("domain", "general")
            if not key:
                continue

            existing = sb_get(
                "knowledge_base",
                f"select=id&domain=eq.{domain}&topic=eq.{key[:100]}&limit=1",
                svc=True
            )
            if existing:
                sb_upsert("pattern_frequency", {"pattern_key": key, "auto_applied": True}, on_conflict="pattern_key")
                continue

            content = _groq_kb_content(key, domain, freq, "real")
            if not content or len(content) < 10:
                print(f"[BACKFILL] Groq returned empty content for: {key[:60]}")
                continue

            ok = sb_post("knowledge_base", {
                "domain":     domain,
                "topic":      key[:100],
                "content":    content,
                "confidence": "medium" if freq < 5 else "high",
                "tags":       ["backfill", "pattern_frequency"],
                "source":     "pattern_frequency",
            })
            if ok:
                inserted += 1
                sb_upsert("pattern_frequency", {"pattern_key": key, "auto_applied": True}, on_conflict="pattern_key")
                print(f"[BACKFILL] KB entry inserted: [{domain}] {key[:60]} (freq={freq})")
            else:
                print(f"[BACKFILL] sb_post failed for: {key[:60]}")

        print(f"[BACKFILL] Done: checked={len(rows)} inserted={inserted}")
        return inserted
    except Exception as e:
        print(f"[BACKFILL] error (non-fatal): {e}")
        return 0


def _run_capability_report():
    """AGI-06/S3: Weekly capability report. Reads last 7 days of capability_metrics,
    computes averages per dimension, sends Telegram summary to owner.
    Non-blocking.
    """
    try:
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        rows = sb_get("capability_metrics",
            f"select=accuracy,efficiency,autonomy,robustness,learning_rate,transfer,composite_score,domain&session_ts=gte.{cutoff}&limit=100",
            svc=True) or []
        if not rows:
            print("[CAP] No capability_metrics entries for last 7 days")
            return
        n = len(rows)
        def avg(key): return round(sum(r.get(key) or 0 for r in rows) / n, 3)
        report = (
            f"\U0001f4ca CORE Capability Report (last 7d, {n} sessions)\n"
            f"Accuracy:      {avg('accuracy')}\n"
            f"Efficiency:    {avg('efficiency')}\n"
            f"Autonomy:      {avg('autonomy')}\n"
            f"Robustness:    {avg('robustness')}\n"
            f"Learning Rate: {avg('learning_rate')}\n"
            f"Transfer:      {avg('transfer')}\n"
            f"Composite:     {avg('composite_score')}"
        )
        notify(report)
        print(f"[CAP] Weekly report sent: composite={avg('composite_score')}")
    except Exception as e:
        print(f"[CAP] report error (non-fatal): {e}")


def _run_memory_consolidation(dry_run: bool = False):
    """AGI-05/S2: Weekly memory consolidation and forgetting job.
    Consolidation: entries with access_count > 10 AND confidence != 'proven' -> boost to 'high'.
    Forgetting: entries with last_accessed > 90 days AND access_count < 2 -> set active=false.
    Non-blocking. Reports via Telegram.
    """
    try:
        now = datetime.utcnow()
        cutoff_90d = (now - timedelta(days=90)).isoformat()

        # Consolidation: frequently accessed entries get confidence boost
        consolidate_rows = sb_get("knowledge_base",
            "select=id,domain,topic,confidence,access_count&id=gt.1&access_count=gte.10&confidence=neq.proven&limit=200",
            svc=True) or []
        consolidated = 0
        for r in consolidate_rows:
            if not dry_run:
                try:
                    sb_patch("knowledge_base", f"id=eq.{r['id']}", {"confidence": "high"})
                    consolidated += 1
                except Exception:
                    pass
            else:
                consolidated += 1

        # Forgetting: stale entries not referenced recently -> soft archive
        forget_rows = sb_get("knowledge_base",
            f"select=id,domain,topic,access_count,last_accessed&id=gt.1&access_count=lt.2&last_accessed=lt.{cutoff_90d}&limit=200",
            svc=True) or []
        # Extra safety: never archive entries referenced in mistakes or behavioral_rules
        archived = 0
        for r in forget_rows:
            topic_slug = (r.get("topic") or "")[:50]
            domain_slug = (r.get("domain") or "")
            # Check if referenced in mistakes
            refs = sb_get("mistakes",
                f"select=id&or=(what_failed.ilike.*{topic_slug}*,correct_approach.ilike.*{topic_slug}*)&limit=1",
                svc=True) or []
            if refs:
                continue  # Referenced in mistakes -- keep
            if not dry_run:
                try:
                    sb_patch("knowledge_base", f"id=eq.{r['id']}", {"active": False})
                    archived += 1
                except Exception:
                    pass
            else:
                archived += 1

        summary = (
            f"[CONSOLIDATION] {'DRY RUN ' if dry_run else ''}Done. "
            f"Consolidated={consolidated} (confidence->high). "
            f"Archived={archived} (active->false, stale 90d+)."
        )
        print(summary)
        if not dry_run and (consolidated + archived) > 0:
            notify(summary)
        return {"ok": True, "consolidated": consolidated, "archived": archived, "dry_run": dry_run}
    except Exception as e:
        print(f"[CONSOLIDATION] error: {e}")
        return {"ok": False, "error": str(e)}


def _run_causal_quality_analysis():
    """AGI-03/S3: When quality trend is declining, analyze last 10 sessions causally.
    Identifies what changed (task types, domains, error rates) and writes explanation
    to knowledge_base(domain=meta). Triggered by run_cold_processor when trend=declining.
    Non-blocking -- never raises.
    """
    try:
        # Check quality trend from last 10 hot_reflections
        recent = sb_get("hot_reflections",
            "select=quality_score,domain,summary,created_at&id=gt.1&order=created_at.desc&limit=10",
            svc=True) or []
        if len(recent) < 5:
            return  # Not enough data

        scores = [float(r.get("quality_score") or 0.7) for r in recent]
        avg_recent = sum(scores[:5]) / 5
        avg_older  = sum(scores[5:]) / max(len(scores[5:]), 1)

        if avg_recent >= avg_older:  # Not actually declining
            return

        # Gather domain distribution
        domain_counts: dict = {}
        for r in recent[:5]:
            d = r.get("domain", "general")
            domain_counts[d] = domain_counts.get(d, 0) + 1
        top_domain = max(domain_counts, key=domain_counts.get) if domain_counts else "unknown"

        # Gather recent mistakes in that domain
        recent_mistakes = sb_get("mistakes",
            f"select=domain,what_failed,severity&domain=eq.{top_domain}&order=created_at.desc&limit=5",
            svc=True) or []
        mistake_summary = "; ".join([
            (m.get("what_failed") or "")[:80] for m in recent_mistakes
        ])

        prompt = (
            f"CORE AGI quality analysis. Recent 5 sessions avg={avg_recent:.2f}, "
            f"prior 5 sessions avg={avg_older:.2f}. Trend: DECLINING.\n"
            f"Top domain in recent sessions: {top_domain}\n"
            f"Recent mistakes in that domain: {mistake_summary[:400]}\n"
            f"In 2-3 sentences: what is the most likely causal explanation for the quality decline? "
            f"Be specific about what changed. Output plain text, no lists."
        )
        explanation = gemini_chat(
            system="You are CORE AGI's causal analyst. Be concise and precise.",
            user=prompt,
            max_tokens=200,
            temperature=0.1,
        )
        if not explanation or len(explanation) < 20:
            return

        today = datetime.utcnow().strftime("%Y-%m-%d")
        sb_post("knowledge_base", {
            "domain": "meta",
            "topic": f"quality_decline_causal_model_{today}",
            "content": (
                f"Quality decline detected {today}. "
                f"Recent avg={avg_recent:.2f} vs prior avg={avg_older:.2f}. "
                f"Top domain: {top_domain}. "
                f"Causal explanation: {explanation}"
            ),
            "confidence": "medium",
        })
        print(f"[COLD][CAUSAL] Quality decline analysis written to KB: {explanation[:100]}")
    except Exception as e:
        print(f"[COLD][CAUSAL] error (non-fatal): {e}")


def run_cold_processor():
    try:
        hots = sb_get("hot_reflections",
                      "select=id,domain,new_patterns,new_mistakes,quality_score,source,task_summary,gaps_identified&processed_by_cold=eq.0&id=gt.1&quality_score=gte.0.5&order=created_at.asc",
                      svc=True)
        skipped_low_quality = sb_get("hot_reflections",
                      "select=id&processed_by_cold=eq.0&id=gt.1&quality_score=lt.0.5",
                      svc=True)
        if skipped_low_quality:
            print(f"[COLD] Skipped {len(skipped_low_quality)} low-quality hots (quality<0.5)")
            for lq in skipped_low_quality:
                sb_patch("hot_reflections", f"id=eq.{lq['id']}", {"processed_by_cold": 1})
        if not hots:
            print("[COLD] No unprocessed hot reflections.")
            return {"ok": True, "processed": 0, "evolutions_queued": 0}

        period_start      = datetime.utcnow().isoformat()
        evolutions_queued = 0
        auto_applied_count = 0
        batch_counts: Counter = Counter()
        batch_domain: dict    = {}
        batch_sources: dict   = {}

        for h in hots:
            src = h.get("source") or "real"
            raw_patterns = h.get("new_patterns") or []
            if isinstance(raw_patterns, str):
                raw_patterns = raw_patterns.strip()
                try:
                    parsed = json.loads(raw_patterns)
                    raw_patterns = parsed if isinstance(parsed, list) else [raw_patterns]
                except (json.JSONDecodeError, ValueError):
                    raw_patterns = [x.strip() for x in raw_patterns.replace("\n", ",").split(",") if x.strip()]
            for p in raw_patterns:
                if p and isinstance(p, str) and len(p) > 3:
                    key = str(p)[:500]
                    batch_counts[key] += 1
                    batch_domain.setdefault(key, h.get("domain", "general"))
                    batch_sources.setdefault(key, set()).add(src)

        if len(batch_counts) > 1:
            batch_counts, batch_domain, batch_sources = _groq_cluster_patterns(
                batch_counts, batch_domain, batch_sources
            )

        all_pf = {r["pattern_key"]: r for r in sb_get(
            "pattern_frequency", "select=id,pattern_key,frequency,auto_applied&limit=2000", svc=True
        ) if r.get("id") != 1 and r.get("pattern_key")}

        for key, batch_count in batch_counts.items():
            existing = all_pf.get(key)
            src_set  = batch_sources.get(key, {"real"})
            src_key  = "both" if len(src_set) > 1 else next(iter(src_set))
            src_mult = _SRC_CONF.get(src_key, 1.0)
            domain   = batch_domain.get(key, "general")

            now_ts = datetime.utcnow().isoformat()
            if existing:
                new_freq = existing["frequency"] + batch_count
                sb_upsert("pattern_frequency",
                          {"id": existing["id"], "pattern_key": key, "frequency": new_freq,
                           "domain": domain, "description": key[:500], "last_seen": now_ts,
                           "stale": False},
                          on_conflict="id")
                total_freq = new_freq
            else:
                sb_upsert("pattern_frequency",
                          {"pattern_key": key, "frequency": batch_count,
                           "domain": domain, "description": key[:500], "auto_applied": False,
                           "last_seen": now_ts, "stale": False},
                          on_conflict="pattern_key")
                total_freq = batch_count

            _milestones = {10, 25, 50, 100, 200, 500}
            _already_applied = (existing or {}).get("auto_applied", False)
            _at_milestone = total_freq in _milestones
            _should_queue = total_freq >= PATTERN_EVO_THRESHOLD and (
                not _already_applied or _at_milestone
            )
            if _should_queue:
                base_conf  = min(0.5 + total_freq * 0.05, 0.95)
                final_conf = round(base_conf * src_mult, 3)
                kb_content = _groq_kb_content(key, domain, total_freq, src_key)
                _already_active = sb_get("evolution_queue",
                    f"select=id,status&pattern_key=eq.{key[:200]}&status=in.(pending,rejected)&limit=1",
                    svc=True)
                if _already_active:
                    _existing_status = _already_active[0].get("status", "pending")
                    if _existing_status == "rejected":
                        print(f"[COLD] Skipped previously-rejected evo: {key[:80]}")
                    else:
                        print(f"[COLD] Skipped duplicate evo (pending): {key[:80]}")
                    continue

                ok = sb_post_critical("evolution_queue", {
                    "change_type":    "knowledge",
                    "change_summary": kb_content[:500],
                    "pattern_key":    key,
                    "confidence":     final_conf,
                    "status":         "pending",
                    "source":         src_key,
                    "impact":         domain,
                    "recommendation": f"Pattern appears {total_freq}x (src={src_key}). KB content Groq-generated.",
                })
                if ok:
                    evolutions_queued += 1
                    sb_upsert("pattern_frequency",
                              {"pattern_key": key, "auto_applied": True},
                              on_conflict="pattern_key")
                    if final_conf >= 0.65 and src_key == "real":
                        new_evo = sb_get("evolution_queue",
                            f"select=id&pattern_key=eq.{key[:100]}&status=eq.pending&order=id.desc&limit=1",
                            svc=True)
                        if new_evo:
                            result = apply_evolution(new_evo[0]["id"])
                            if result.get("ok"):
                                auto_applied_count += 1
                                print(f"[COLD] Auto-applied evolution #{new_evo[0]['id']}: {key[:80]}")

        gaps_inserted = _reconcile_gaps(hots)
        evolutions_queued += gaps_inserted
        _backfill_patterns(batch_size=10)

        groq_summary = _groq_synthesize_cold(hots, batch_counts, batch_domain)
        counter_suffix = f" | hots={len(hots)} patterns={len(batch_counts)} evos={evolutions_queued}"
        summary_text = groq_summary + counter_suffix

        sb_post_critical("cold_reflections", {
            "period_start": period_start, "period_end": datetime.utcnow().isoformat(),
            "hot_count": len(hots), "patterns_found": len(batch_counts),
            "evolutions_queued": evolutions_queued, "auto_applied": auto_applied_count,
            "summary_text": summary_text[:2000],
        })
        for h in hots:
            sb_patch("hot_reflections", f"id=eq.{h['id']}", {"processed_by_cold": 1})
        if evolutions_queued > 0:
            notify(f"Cold processor: {evolutions_queued} evolution(s) queued, {auto_applied_count} auto-applied.\n{groq_summary[:300]}\nPending owner review: {evolutions_queued - auto_applied_count}")
        # AGI-03/S3: trigger causal quality analysis if recent quality is declining
        try:
            recent_scores = [float(h.get("quality_score") or 0.7) for h in hots[-10:]]
            if len(recent_scores) >= 5:
                avg5 = sum(recent_scores[-5:]) / 5
                avg_all = sum(recent_scores) / len(recent_scores)
                if avg5 < avg_all - 0.05:  # Recent 5 meaningfully worse than session average
                    print(f"[COLD] Quality declining (recent_avg={avg5:.2f} vs overall={avg_all:.2f}) -- running causal analysis")
                    _run_causal_quality_analysis()
        except Exception:
            pass

        print(f"[COLD] Done: processed={len(hots)} patterns={len(batch_counts)} evolutions={evolutions_queued} auto_applied={auto_applied_count}")
        return {"ok": True, "processed": len(hots), "patterns_found": len(batch_counts), "evolutions_queued": evolutions_queued, "auto_applied": auto_applied_count}
    except Exception as e:
        print(f"[COLD] error: {e}")
        return {"ok": False, "error": str(e)}


# -- Evolution apply/reject ----------------------------------------------------
def apply_evolution(evolution_id: int):
    try:
        rows = sb_get("evolution_queue",
                      f"select=*&id=eq.{evolution_id}&status=eq.pending&limit=1", svc=True)
        if not rows:
            return {"ok": False, "error": f"Evolution {evolution_id} not found or not pending"}
        evo           = rows[0]
        change_type   = evo.get("change_type", "knowledge")
        change_summary= evo.get("change_summary", "")
        diff_content  = evo.get("diff_content", "")
        pattern_key   = evo.get("pattern_key", "")
        confidence    = float(evo.get("confidence") or 0.5)
        applied = False; note = ""

        if change_type == "knowledge":
            applied = sb_upsert("knowledge_base", {
                "domain": evo.get("impact", "general"),
                "topic": (pattern_key[:100] or change_summary[:100]),
                "content": change_summary,
                "confidence": "high" if confidence >= 0.8 else "medium",
                "tags": ["evolution", "auto"], "source": "evolution_queue",
            }, on_conflict="domain,topic")
            note = "Added/updated knowledge_base"

        elif change_type == "new_tool":
            try:
                meta = json.loads(diff_content) if diff_content else {}
            except Exception:
                meta = {}
            fn_name = meta.get("fn_name", "")
            fn_code  = meta.get("code", "")
            if not fn_name or not fn_code:
                prompt = (f"Write a Python function for CORE AGI system named '{pattern_key or 'new_tool'}'.\n"
                          f"Purpose: {change_summary}\n"
                          f"Recommendation: {evo.get('recommendation','')}\n\n"
                          f"Rules:\n"
                          f"- Use sb_post, sb_get, sb_patch, groq_chat, gh_read, gh_write as needed\n"
                          f"- Return dict with 'ok' key always\n"
                          f"- Add docstring explaining purpose\n"
                          f"- Follow CORE naming: t_<n>\n\n"
                          f"Output ONLY the Python function, no explanation.")
                fn_code = groq_chat(
                    "You are CORE's code generation engine. Output only valid Python. No markdown, no preamble.",
                    prompt, model=GROQ_MODEL, max_tokens=600
                )
                fn_name = ""
                for line in fn_code.splitlines():
                    if line.strip().startswith("def "):
                        fn_name = line.strip().split("(")[0].replace("def ", "").strip()
                        break
            if fn_name and fn_code:
                sb_patch("evolution_queue", f"id=eq.{evolution_id}", {"status": "pending_desktop"})
                notify(f"[NEW TOOL] Evolution #{evolution_id} generated code for '{fn_name}'.\n"
                       f"Apply via Claude Desktop: add to core_tools.py + register in TOOLS dict.")
                applied = True
                note = f"New tool '{fn_name}' code generated — needs Desktop apply to core_tools.py"
                sb_post("script_templates", {
                    "name": fn_name, "description": change_summary[:200],
                    "trigger_pattern": evo.get("recommendation", ""),
                    "code": fn_code, "use_count": 0,
                    "created_at": datetime.utcnow().isoformat(),
                })
            else:
                note = "new_tool evolution: could not extract function name from generated code"
                applied = False

        elif change_type == "script_template":
            try:
                meta = json.loads(diff_content) if diff_content else {}
            except Exception:
                meta = {}
            tpl_name = meta.get("name", pattern_key or f"template_{evolution_id}")
            tpl_code = meta.get("code", change_summary)
            applied = sb_post("script_templates", {
                "name": tpl_name,
                "description": meta.get("description", change_summary[:200]),
                "trigger_pattern": meta.get("trigger_pattern", evo.get("recommendation", "")),
                "code": tpl_code, "use_count": 0,
                "created_at": datetime.utcnow().isoformat(),
            })
            note = f"Script template '{tpl_name}' stored in Supabase"

        elif change_type == "code":
            if not diff_content: return {"ok": False, "error": "code evolution requires diff_content"}
            fname = f"patches/evo_{evolution_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.patch"
            applied = gh_write(fname, diff_content, f"Evolution #{evolution_id}: {change_summary[:60]}")
            note = f"Patch written to {fname}"

        elif change_type == "behavior":
            if not diff_content: return {"ok": False, "error": "behavior evolution requires diff_content"}
            applied = gh_write("BEHAVIOR_UPDATES.md", diff_content,
                               f"Behavior evolution #{evolution_id}: {change_summary[:60]}")
            note = "Written to BEHAVIOR_UPDATES.md"

        elif change_type == "backlog":
            reject_evolution(evolution_id, reason="backlog change_type retired — owner decides backlog", silent=True)
            return {"ok": False, "evolution_id": evolution_id, "change_type": change_type,
                    "note": "backlog change_type retired — auto-rejected"}

        if applied:
            sb_patch("evolution_queue", f"id=eq.{evolution_id}",
                     {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
            notify(f"Evolution #{evolution_id} applied\nType: {change_type}\n{note}")
        else:
            if change_type not in ("backlog",):
                notify(f"Evolution #{evolution_id} apply failed\nType: {change_type}")
            else:
                print(f"[EVO] #{evolution_id} backlog apply failed silently")
        return {"ok": applied, "evolution_id": evolution_id, "change_type": change_type, "note": note}
    except Exception as e:
        print(f"[EVO] error: {e}")
        return {"ok": False, "error": str(e)}


def reject_evolution(evolution_id: int, reason: str = "", silent: bool = False):
    try:
        rows = sb_get("evolution_queue",
                      f"select=*&id=eq.{evolution_id}&status=in.(pending,synthesized)&limit=1", svc=True)
        if not rows: return {"ok": False, "error": f"Evolution {evolution_id} not found or not pending/synthesized"}
        sb_patch("evolution_queue", f"id=eq.{evolution_id}", {"status": "rejected"})
        if not silent:
            sb_post("mistakes", {
                "domain": "evolution", "context": f"Evolution #{evolution_id}: {rows[0].get('change_summary','')[:200]}",
                "what_failed": "Evolution rejected by owner",
                "correct_approach": reason or "Owner rejected - review pattern and confidence threshold",
                "root_cause": reason or "Unknown",
                "how_to_avoid": "Raise confidence threshold or improve pattern quality",
                "severity": "low", "tags": ["evolution", "rejected"],
            })
            notify(f"Evolution #{evolution_id} rejected.\nReason: {reason or 'No reason given'}")
        return {"ok": True, "evolution_id": evolution_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def bulk_reject_evolutions(change_type: str = "", ids: list = None, reason: str = "", include_synthesized: bool = False) -> dict:
    try:
        statuses = "status=in.(pending,synthesized)" if include_synthesized else "status=eq.pending"
        if ids:
            qs = f"select=id&{statuses}&id=in.({','.join(str(i) for i in ids)})"
        elif change_type:
            qs = f"select=id&{statuses}&change_type=eq.{change_type}"
        else:
            qs = f"select=id&{statuses}"
        rows = sb_get("evolution_queue", qs + "&limit=500", svc=True)
        rejected = 0
        skipped  = 0
        for row in rows:
            result = reject_evolution(row["id"], reason=reason, silent=True)
            if result.get("ok"):
                rejected += 1
            else:
                skipped += 1
        summary = f"Bulk rejected {rejected} evolutions (type={change_type or 'all'}, skipped={skipped}). Reason: {reason or 'none'}"
        print(f"[EVOLUTION] {summary}")
        notify(summary)
        return {"ok": True, "rejected": rejected, "skipped": skipped}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- TASK-9.D: Dead Pattern Pruner -------------------------------------------
_last_stale_check: float = 0.0
_STALE_CHECK_INTERVAL = 86400  # 24h
_STALE_DAYS = 30

def _check_stale_patterns() -> int:
    try:
        cutoff = (datetime.utcnow() - timedelta(days=_STALE_DAYS)).isoformat()
        stale_rows = sb_get(
            "pattern_frequency",
            f"select=id,pattern_key&last_seen=lt.{cutoff}&auto_applied=eq.true",
            svc=True
        ) or []
        count = 0
        for row in stale_rows:
            try:
                sb_patch("pattern_frequency", f"id=eq.{row['id']}", {"stale": True})
                count += 1
            except Exception as _e:
                print(f"[STALE] patch error for id={row['id']}: {_e}")
        if count:
            print(f"[STALE] Marked {count} patterns as stale (not seen in {_STALE_DAYS}d)")
        return count
    except Exception as e:
        print(f"[STALE] _check_stale_patterns error: {e}")
        return 0


# -- Cold processor loop -------------------------------------------------------
def _restore_diag_timestamp():
    """Seed _last_self_diagnosis_run from Supabase on boot.
    Prevents Railway redeploy from resetting the 24h guard to zero.
    Falls back to (now - 25h) so first real 19:00 UTC window triggers normally."""
    global _last_self_diagnosis_run
    try:
        rows = sb_get(
            "sessions",
            f"select=state_value&state_key=eq.{_DIAG_STATE_KEY}&order=id.desc&limit=1",
            svc=True
        ) or []
        if rows:
            stored = float(rows[0].get("state_value") or 0)
            if stored > 0:
                _last_self_diagnosis_run = stored
                print(f"[DIAG] Restored last_diag_ts from Supabase: {datetime.utcfromtimestamp(stored).isoformat()}")
                return
    except Exception as e:
        print(f"[DIAG] restore error: {e}")
    # Fallback: treat as ran 25h ago so it will fire at next scheduled window, not immediately
    _last_self_diagnosis_run = time.time() - (_SELF_DIAGNOSIS_INTERVAL + 3600)
    print("[DIAG] No stored timestamp -- seeded to 25h ago (will fire at next 19:00 UTC window)")

def cold_processor_loop():
    global _last_cold_run, _last_cold_kb_count, _last_stale_check
    print("[COLD] Background loop started")
    _restore_diag_timestamp()  # AGI-02: seed from Supabase before first loop tick
    while True:
        try:
            hots        = sb_get("hot_reflections", "select=id&processed_by_cold=eq.0&id=gt.1", svc=True)
            unprocessed = len(hots)
            time_since  = time.time() - _last_cold_run

            current_kb_count = 0
            try:
                counts = get_system_counts()
                current_kb_count = counts.get("knowledge_base", 0)
            except Exception:
                pass
            kb_growth = current_kb_count - _last_cold_kb_count

            should_run = (
                unprocessed >= COLD_HOT_THRESHOLD or
                (time_since >= COLD_TIME_THRESHOLD and unprocessed > 0) or
                (kb_growth >= COLD_KB_GROWTH_THRESHOLD and _last_cold_kb_count > 0)
            )

            if should_run:
                trigger = (
                    f"unprocessed={unprocessed}" if unprocessed >= COLD_HOT_THRESHOLD else
                    f"kb_growth={kb_growth}" if kb_growth >= COLD_KB_GROWTH_THRESHOLD else
                    f"time_since={int(time_since)}s"
                )
                print(f"[COLD] Triggering: {trigger}")
                run_cold_processor()
                _last_cold_run = time.time()
                _last_cold_kb_count = current_kb_count

            for evo in sb_get("evolution_queue",
                               "select=id,confidence,change_type&status=eq.pending&change_type=eq.knowledge&id=gt.1",
                               svc=True):
                conf = float(evo.get("confidence") or 0)
                if conf >= KNOWLEDGE_AUTO_CONFIDENCE:
                    apply_evolution(evo["id"])
            if time.time() - _last_stale_check >= _STALE_CHECK_INTERVAL:
                _check_stale_patterns()
                _last_stale_check = time.time()
            # AGI-02: Nightly self-diagnosis at 02:00 WIB (19:00 UTC)
            try:
                now_utc = datetime.utcnow()
                # Guard: if timestamp still sentinel (-1), restore before checking
                if _last_self_diagnosis_run < 0:
                    _restore_diag_timestamp()
                time_since_diag = time.time() - _last_self_diagnosis_run
                if (now_utc.hour == _SELF_DIAGNOSIS_HOUR_UTC and
                        time_since_diag >= _SELF_DIAGNOSIS_INTERVAL):
                    _run_self_diagnosis()
                    # PHASE-M: Run tool health scan after self-diagnosis (same nightly window)
                    try:
                        from core_tools import t_tool_health_scan
                        t_tool_health_scan(force="false")
                        print("[DIAG] tool_health_scan complete")
                    except Exception as _ths_e:
                        print(f"[DIAG] tool_health_scan error: {_ths_e}")
            except Exception as _de:
                print(f"[DIAG] trigger error: {_de}")
            # AGI-01: Weekly cross-domain synthesis on Wednesday
            try:
                now_utc = datetime.utcnow()
                time_since_synth = time.time() - _last_synthesis_run
                if (now_utc.weekday() == _SYNTHESIS_DAY_UTC and
                        time_since_synth >= _SYNTHESIS_INTERVAL):
                    _run_cross_domain_synthesis()
            except Exception as _se:
                print(f"[SYNTH] trigger error: {_se}")
            # AGI-05: Weekly memory consolidation on Sunday
            try:
                global _last_consolidation_run
                now_utc = datetime.utcnow()
                time_since_consol = time.time() - _last_consolidation_run
                if (now_utc.weekday() == _CONSOLIDATION_DAY_UTC and
                        time_since_consol >= _CONSOLIDATION_INTERVAL):
                    _run_memory_consolidation()
                    _last_consolidation_run = time.time()
            except Exception as _ce:
                print(f"[CONSOLIDATION] trigger error: {_ce}")
            # AGI-06: Weekly capability report on Tuesday
            try:
                global _last_capability_report_run
                now_utc = datetime.utcnow()
                time_since_cap = time.time() - _last_capability_report_run
                if (now_utc.weekday() == _CAPABILITY_REPORT_DAY_UTC and
                        time_since_cap >= _CAPABILITY_REPORT_INTERVAL):
                    _run_capability_report()
                    _last_capability_report_run = time.time()
            except Exception as _cape:
                print(f"[CAP] trigger error: {_cape}")
            # GAP-DATA-01: Weekly backup check -- runs if last backup > 6 days ago
            try:
                _BACKUP_INTERVAL = 6 * 24 * 3600  # 6 days in seconds
                last_backup_rows = sb_get("sessions",
                    "select=summary&summary=like.*last_backup_ts*&order=created_at.desc&limit=1",
                    svc=True)
                last_backup_ts = 0
                if last_backup_rows:
                    import re as _re2
                    m = _re2.search(r'last_backup_ts: ([\d\-T:.]+)', last_backup_rows[0].get("summary", ""))
                    if m:
                        from datetime import datetime as _dt2
                        try: last_backup_ts = _dt2.fromisoformat(m.group(1)).timestamp()
                        except Exception: last_backup_ts = 0
                if time.time() - last_backup_ts >= _BACKUP_INTERVAL:
                    print("[COLD] Weekly backup triggered")
                    from core_tools import TOOLS as _T
                    _T["backup_brain"]["fn"]()
            except Exception as _be:
                print(f"[COLD] backup check error: {_be}")
        except Exception as e:
            print(f"[COLD] loop error: {e}")
        time.sleep(1800)


# -- Backlog helpers -----------------------------------------------------------
def _backlog_add(items: list) -> list:
    try:
        existing_rows = sb_get("backlog", "select=title&order=id.asc&limit=500", svc=True)
        existing_titles = {r.get("title", "").lower() for r in existing_rows}
    except Exception as e:
        print(f"[BACKLOG] fetch existing error: {e}")
        existing_titles = set()

    new_items = []
    for item in items:
        title = item.get("title", "").strip()
        if not title or title.lower() in existing_titles:
            continue
        existing_titles.add(title.lower())
        priority = int(item.get("priority", 1))
        itype    = item.get("type", "other")
        effort   = item.get("effort", "medium")
        domain   = item.get("domain", "general")

        ok = sb_post("backlog", {
            "title":        title,
            "type":         itype,
            "priority":     priority,
            "description":  item.get("description", "")[:500],
            "domain":       domain,
            "effort":       effort,
            "impact":       item.get("impact", "medium"),
            "status":       "pending",
            "discovered_at": item.get("discovered_at", datetime.utcnow().isoformat()),
        })
        if ok:
            new_items.append(item)
    return new_items


def _sync_backlog_status():
    return 0


def _backlog_to_markdown() -> str:
    return "# BACKLOG.md deprecated - use get_backlog MCP tool or Supabase directly."


# -- KB Mining -----------------------------------------------------------------
def run_kb_mining(max_batches: int = 50, force: bool = False) -> dict:
    print("[KB MINE] deprecated - backlog table dropped, no-op")
    return {"ok": False, "deprecated": True, "reason": "backlog table dropped - use evolution_queue pipeline instead"}


# -- Real signal + simulation --------------------------------------------------
def _extract_real_signal() -> bool:
    try:
        state_rows = sb_get("sessions",
            "select=summary&summary=like.*last_real_signal_ts:*&order=created_at.desc&limit=1",
            svc=True)
        if state_rows and state_rows[0].get("summary"):
            raw = state_rows[0]["summary"].split("last_real_signal_ts:")[-1].strip().split()[0]
            since_ts = raw.replace("Z", "").split("+")[0]
            print(f"[RESEARCH/REAL] Using last_real_signal_ts: {since_ts}")
        else:
            since_ts = (datetime.utcnow() - timedelta(days=1)).isoformat()
            print(f"[RESEARCH/REAL] No state key found, soft-boot to yesterday: {since_ts}")

        sessions = sb_get("sessions",
            f"select=summary,actions,interface&created_at=gte.{since_ts}&order=created_at.desc&limit=20",
            svc=True)
        mistakes = sb_get("mistakes",
            f"select=domain,what_failed,root_cause,how_to_avoid&created_at=gte.{since_ts}&order=id.desc&limit=20",
            svc=True)

        if not sessions and not mistakes:
            since_ts = (datetime.utcnow() - timedelta(days=7)).isoformat()
            print(f"[RESEARCH/REAL] No new data since anchor -- falling back to 7d window: {since_ts}")
            sessions = sb_get("sessions",
                f"select=summary,actions,interface&created_at=gte.{since_ts}&order=created_at.desc&limit=20",
                svc=True)
            mistakes = sb_get("mistakes",
                f"select=domain,what_failed,root_cause,how_to_avoid&created_at=gte.{since_ts}&order=id.desc&limit=20",
                svc=True)
            if not sessions and not mistakes:
                print("[RESEARCH/REAL] Still no data in 7d window -- skipping")
                return False

        try:
            kb_count_r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=8)
            kb_total = int(kb_count_r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            kb_total = 0

        try:
            changelog_rows = sb_get("changelog",
                "select=summary,category&order=id.desc&limit=5", svc=True)
            changelog_text = "\n".join(
                f"  [{r.get('category','?')}] {r.get('summary','')[:120]}"
                for r in changelog_rows
            ) if changelog_rows else "None yet."
        except Exception:
            changelog_text = "Unavailable."

        sessions_text = "\n".join([
            f"- [{r.get('interface','?')}] {r.get('summary','')[:200]}"
            for r in sessions
        ]) or "No sessions yet."

        mistakes_text = "\n".join([
            f"- [{r.get('domain','?')}] FAILED: {r.get('what_failed','')[:150]} | ROOT: {r.get('root_cause','')[:100]}"
            for r in mistakes
        ]) or "No mistakes yet."

        system = """You are CORE's pattern extraction engine. Analyze real activity logs and output BEHAVIORAL DIRECTIVES not observations.
Patterns must be actionable rules: what CORE should DO differently, not just what happened.
Output MUST be valid JSON:
{
  "domain": "code|db|bot|mcp|training|kb|general",
  "patterns": ["CORE should X when Y", "Always Z before W"],
  "gaps": "1-2 sentences describing what CORE is missing",
  "summary": "1 sentence behavioral directive"
}
Output ONLY valid JSON, no preamble."""

        user = (f"KB total entries: {kb_total}\n"
                f"Recent changelog:\n{changelog_text}\n\n"
                f"RECENT SESSIONS (since last processed, {len(sessions)} entries):\n{sessions_text}\n\n"
                f"RECENT MISTAKES (since last processed, {len(mistakes)} entries):\n{mistakes_text}\n\n"
                f"Extract behavioral directives from this recent activity. Focus on what CORE should do differently.")

        raw = gemini_chat(system, user, max_tokens=800, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)

        patterns = result.get("patterns", [])
        if not patterns:
            print("[RESEARCH/REAL] Gemini returned no patterns")
            return False

        _gaps_raw = result.get("gaps") or None
        _gaps_list = [_gaps_raw] if _gaps_raw and isinstance(_gaps_raw, str) else _gaps_raw
        _quality = round(min(1.0, max(0.4, 0.5 + len(patterns) * 0.1 + (0.1 if mistakes else 0))), 2)
        ok = sb_post("hot_reflections", {
            "task_summary": f"Real signal extraction (since last processed) - {len(sessions)} sessions, {len(mistakes)} mistakes, kb={kb_total}",
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": _gaps_list,
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": 0,
            "source": "real",
            "quality_score": _quality,
        })
        print(f"[RESEARCH/REAL] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        if ok:
            run_ts = datetime.utcnow().isoformat()
            sb_post("sessions", {
                "summary": f"[state_update] last_real_signal_ts: {run_ts}",
                "actions": [f"last_real_signal_ts={run_ts} - auto updated after real signal extraction"],
                "interface": "mcp"
            })
            print(f"[RESEARCH/REAL] Saved last_real_signal_ts: {run_ts}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/REAL] error: {e}")
        return False


def _get_simulation_task() -> dict:
    try:
        rows = sb_get("sessions",
            "select=summary&summary=like.*simulation_task*&order=created_at.desc&limit=5",
            svc=True)
        for row in rows:
            summary = row.get("summary", "")
            if "[state_update] simulation_task:" not in summary:
                continue
            raw_val = summary.split("[state_update] simulation_task:")[-1].strip()
            if raw_val.lower() == "null":
                return None
            task = json.loads(raw_val)
            if task and task.get("instruction"):
                return task
    except Exception as e:
        print(f"[SIM] _get_simulation_task error: {e}")
    return None


def _run_simulation_batch() -> bool:
    try:
        try:
            from core_tools import TOOLS
            tool_list = list(TOOLS.keys())
        except ImportError:
            tool_list = []

        mistakes = sb_get("mistakes",
            "select=domain,what_failed&order=id.desc&limit=10", svc=True)
        kb_sample = sb_get("knowledge_base",
            "select=domain,topic&order=id.desc&limit=20", svc=True)

        failure_modes = "\n".join([
            f"- [{r.get('domain','?')}] {r.get('what_failed','')[:120]}"
            for r in mistakes
        ]) or "None recorded yet."

        kb_domains = list({r.get("domain", "general") for r in kb_sample})
        kb_topics_sample = [r.get("topic", "") for r in kb_sample[:10]]

        try:
            kb_count_r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=8)
            kb_total = int(kb_count_r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            kb_total = len(kb_sample)

        try:
            recent_evos = sb_get("evolution_queue",
                "select=change_type,change_summary&status=eq.applied&order=id.desc&limit=5",
                svc=True)
            evos_text = "\n".join(
                f"  [{r.get('change_type','?')}] {r.get('change_summary','')[:100]}"
                for r in recent_evos
            ) if recent_evos else "None yet."
        except Exception:
            evos_text = "Unavailable."

        runtime_context = (
            f"CORE MCP tools ({len(tool_list)}): {', '.join(tool_list[:20])}\n"
            f"KB total entries: {kb_total}\n"
            f"Recently applied evolutions:\n{evos_text}\n"
            f"Known failure modes:\n{failure_modes}\n"
            f"KB domains: {', '.join(kb_domains)}\n"
            f"Sample KB topics: {', '.join(kb_topics_sample)}"
        )

        task = _get_simulation_task()

        if task:
            instruction = task.get("instruction", "")
            system = task.get("system_prompt", "")
            user_template = task.get("user_prompt_template", "")
            user = user_template.replace("{{RUNTIME_CONTEXT}}", runtime_context)
            task_summary = f"Custom simulation: {instruction[:150]}"
            print(f"[RESEARCH/SIM] Running custom simulation: {instruction[:80]}")
        else:
            system = """You are simulating 1,000,000 users of CORE - a personal AGI orchestration system.
Output MUST be valid JSON:
{
  "domain": "code|db|bot|mcp|training|kb|general",
  "patterns": ["pattern1", "pattern2"],
  "gaps": "1-2 sentences",
  "summary": "1 sentence"
}
Output ONLY valid JSON, no preamble."""
            user = (f"{runtime_context}\n\nSimulate 1,000,000 users. What patterns emerge?")
            task_summary = "Simulated 1M user population batch"
            print("[RESEARCH/SIM] Running default 1M user simulation")

        raw = gemini_chat(system, user, max_tokens=900, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(raw)

        patterns = result.get("patterns", [])
        if not patterns:
            print("[RESEARCH/SIM] Gemini returned no patterns")
            return False

        _sim_gaps_raw = result.get("gaps") or None
        _sim_gaps_list = [_sim_gaps_raw] if _sim_gaps_raw and isinstance(_sim_gaps_raw, str) else _sim_gaps_raw
        _sim_quality = round(min(1.0, max(0.4, 0.5 + len(patterns) * 0.08)), 2)
        ok = sb_post("hot_reflections", {
            "task_summary": task_summary,
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": _sim_gaps_list,
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": 0,
            "source": "simulation",
            "quality_score": _sim_quality,
        })
        print(f"[RESEARCH/SIM] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/SIM] error: {e}")
        return False


# -- RARL epoch ----------------------------------------------------------------

def _run_rarl_epoch() -> bool:
    """Run one RARL research epoch via Gemini (Groq fallback).
    Replaces _run_simulation_batch() call in background_researcher().
    Writes to: rarl_architectures, rarl_epochs, hot_reflections (domain=rarl),
    mistakes (critic failures), knowledge_base (compressed insight),
    evolution_queue (significant discoveries > 0.3 DS improvement).
    Returns True if hot_reflection written successfully.
    """
    import time as _time
    _start = _time.time()

    # Step 1: epoch number
    try:
        last = sb_get(
            "rarl_epochs",
            "select=epoch_number&id=gt.1&order=epoch_number.desc&limit=1",
            svc=True
        )
        epoch_number = (last[0]["epoch_number"] + 1) if last else 1
    except Exception as e:
        print(f"[RARL] epoch_number query error: {e}")
        epoch_number = 1
    print(f"[RARL] Starting epoch {epoch_number}")

    # Step 2: live context
    try:
        kb_count_r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=8
        )
        kb_total = int(kb_count_r.headers.get("content-range", "*/0").split("/")[-1])
    except Exception:
        kb_total = 0
    try:
        recent_mistakes = sb_get(
            "mistakes", "select=domain,what_failed&order=id.desc&limit=10&id=gt.1", svc=True
        ) or []
    except Exception:
        recent_mistakes = []
    try:
        champion = sb_get(
            "rarl_architectures",
            "select=arch_id,hypothesis,discovery_score,next_direction"
            "&role=eq.champion&id=gt.1&order=created_at.desc&limit=1",
            svc=True
        )
        champion = champion[0] if champion else None
    except Exception:
        champion = None
    try:
        recent_kb = sb_get(
            "knowledge_base",
            "select=topic,instruction&domain=eq.rarl&id=gt.1&order=updated_at.desc&limit=10",
            svc=True
        ) or []
    except Exception:
        recent_kb = []

    # Step 3: research goal -- 6-epoch rotation, custom instruction override
    _RARL_GOALS = [
        ("improve reasoning depth -- focus on sustained multi-step logical reasoning chains that do not degrade over long contexts", "reasoning"),
        ("improve memory architecture -- focus on continual learning mechanisms that prevent catastrophic forgetting across tasks", "memory"),
        ("improve world modeling -- focus on building predictive internal models of environment dynamics for better planning", "world_modeling"),
        ("improve sample efficiency -- focus on architectures that learn robust representations from severely limited training data", "sample_efficiency"),
        ("improve cross-domain generalization -- focus on knowledge transfer mechanisms that apply learned skills to unseen domains", "generalization"),
        ("improve planning capability -- focus on multi-step goal-directed reasoning with explicit internal search and evaluation", "planning"),
    ]
    _custom_goal = None
    try:
        _task = _get_simulation_task()
        if _task and _task.get("instruction") and "RARL EPOCH" not in _task["instruction"]:
            _custom_goal = _task["instruction"][:300]
    except Exception:
        pass
    if _custom_goal:
        research_goal, research_domain = _custom_goal, "general"
    else:
        research_goal, research_domain = _RARL_GOALS[(epoch_number - 1) % len(_RARL_GOALS)]

    ds_before = champion["discovery_score"] if champion else 0.0
    failure_block = "\n".join(
        f"  - [{r.get('domain','?')}] {r.get('what_failed','')[:100]}" for r in recent_mistakes
    ) or "  None recorded yet."
    kb_block = "\n".join(
        f"  - {r.get('topic','')[:100]}" for r in recent_kb
    ) or "  None yet."
    champion_block = (
        f"Champion: {champion['arch_id']} | DS: {champion['discovery_score']:.3f}\n"
        f"  Next direction: {champion.get('next_direction','not set')[:200]}"
        if champion else "No champion yet -- establish a strong baseline."
    )

    # Step 4: build prompts
    _sys = (
        "You are the Recursive Autonomous AGI Research Laboratory (RARL). "
        "You simulate 10 specialized agents: Planner, Architect, Theory, Literature, "
        "Critic, Experiment, Evaluation, Archivist, Meta-Learning, Prompt Evolution. "
        "Target architectures that advance AGI. Be technically grounded. "
        "Mark uncertain reasoning [CONJECTURE]. Output ONLY valid JSON."
    )
    _json_schema = (
        '{"research_goal":"<one sentence confirming goal>",'
        '"hypothesis":"<2-4 sentence architectural hypothesis>",'
        '"core_mechanism":"<4-6 sentence technical description>",'
        '"pseudocode":"<15-25 lines Python style>",'
        '"mutation_applied":"<one of: SynapticPruning|TopologyExpansion|ModularDuplication|CrossDomainGrafting|MemoryAugmentation|LearningRuleMutation|DynamicRoutingMutation|WorldModelIntegration|NeuroSymbolicIntegration|SparseExpertRouting|Novel>",'
        '"theory_analysis":"<2-3 sentences with [CONJECTURE] markers>",'
        '"experiment_design":"<real benchmarks, compute estimate in GPU-hours, numeric success criteria>",'
        '"critic_failures":["<specific technical failure 1>","<specific technical failure 2>","<specific technical failure 3>"],'
        '"mitigation":"<how failures are addressed>",'
        '"benchmark_score":<REAL float 0.0-3.0 — your estimate of benchmark performance>,'
        '"transfer_score":<REAL float 0.0-3.0 — cross-domain transfer ability>,'
        '"stability_score":<REAL float 0.0-3.0 — training stability>,'
        '"sample_efficiency":<REAL float 0.0-3.0 — learning from limited data>,'
        '"reasoning_depth":<REAL float 0.0-3.0 — multi-step reasoning depth>,'
        '"complexity_penalty":<REAL float 0.5-3.0 — implementation complexity cost>,'
        '"compute_cost":<REAL float 0.5-3.0 — training/inference cost>,'
        '"inference_latency":<REAL float 0.5-3.0 — response speed penalty>,'
        '"discovery_score":<REAL float — compute DS = sum(numerators)/sum(denominators)>,'
        '"beats_champion":<true if DS > ' + f'{ds_before:.3f}' + ', else false>,'
        f'"arch_id":"<DescriptiveName_v{epoch_number} — e.g. SparseGating_MemAug_v{epoch_number}>",'
        '"compressed_insight":"<one specific sentence distinct from prior KB — name the mechanism>",'
        '"next_direction":"<specific next epoch direction — what mechanism to explore next>",'
        '"insight_for_core":"<one actionable change to core_tools.py/core_train.py/behavioral_rules/schema — be specific>",'
        '"meta_learning_note":"<one concrete RARL methodology improvement>",'
        '"prompt_evolution_note":"<one change to improve evolution_queue quality, or null>"}'
    )
    _usr = (
        f"EPOCH: {epoch_number}\n"
        f"RESEARCH GOAL: {research_goal}\n"
        f"DOMAIN: {research_domain}\n\n"
        f"LIVE STATE:\n"
        f"  KB entries: {kb_total}\n"
        f"  Recent mistakes:\n{failure_block}\n"
        f"  RARL KB (do not repeat):\n{kb_block}\n"
        f"  {champion_block}\n\n"
        f"Discovery Score: DS = (benchmark+transfer+stability+sample_efficiency+reasoning_depth)"
        f" / (complexity_penalty+compute_cost+inference_latency)\n"
        f"Numerators 0.0-3.0. Denominators 0.5-3.0. beats_champion=true only if DS>{ds_before:.3f}\n"
        f"arch_id format: DescriptiveMechanism_v{epoch_number}\n\n"
        f"Output ONLY this JSON:\n{_json_schema}"
    )

    # Step 5: call Gemini, fallback to Groq
    parsed = {}
    try:
        raw = gemini_chat(_sys, _usr, max_tokens=2048, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(raw)
        print(f"[RARL] Gemini OK epoch={epoch_number} DS={parsed.get('discovery_score',0):.3f}")
    except Exception as e_gem:
        print(f"[RARL] Gemini failed ({e_gem}), trying Groq fallback")
        try:
            raw = groq_chat(_sys, _usr, model=GROQ_MODEL, max_tokens=1500)
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            parsed = json.loads(raw)
            print(f"[RARL] Groq OK epoch={epoch_number} DS={parsed.get('discovery_score',0):.3f}")
        except Exception as e_grq:
            print(f"[RARL] both LLMs failed: {e_grq}")
            sb_post("hot_reflections", {
                "task_summary": f"RARL Epoch {epoch_number} -- LLM error: {str(e_grq)[:100]}",
                "domain": "rarl", "new_patterns": [], "quality_score": 0.1,
                "gaps_identified": ["Gemini and Groq both failed"],
                "reflection_text": f"Epoch {epoch_number} LLM failure. gem={e_gem} grq={e_grq}",
                "processed_by_cold": 0, "source": "rarl",
            })
            return False

    # Step 6: extract fields
    arch_id          = parsed.get("arch_id", f"Unknown_v{epoch_number}")
    hypothesis       = parsed.get("hypothesis", "")[:1000]
    core_mechanism   = parsed.get("core_mechanism", "")[:1000]
    pseudocode       = parsed.get("pseudocode", "")[:2000]
    mutation_applied = parsed.get("mutation_applied", "Unknown")
    critic_failures  = parsed.get("critic_failures", [])[:5]
    mitigation       = parsed.get("mitigation", "")[:500]
    next_direction   = parsed.get("next_direction", "")[:300]
    insight_for_core = parsed.get("insight_for_core", "")[:300]
    compressed       = parsed.get("compressed_insight", "")[:400]
    beats_champion   = bool(parsed.get("beats_champion", False))
    ds               = float(parsed.get("discovery_score", 0.0))
    role             = "champion" if beats_champion else "mutant"
    duration         = int(_time.time() - _start)

    # Step 7: update rarl_architectures (upsert on arch_id to handle duplicate names)
    try:
        if beats_champion and champion:
            sb_patch("rarl_architectures", "role=eq.champion", {"role": "archived"})
        sb_upsert("rarl_architectures", {
            "arch_id": arch_id, "epoch_created": epoch_number, "role": role,
            "hypothesis": hypothesis, "core_mechanism": core_mechanism, "pseudocode": pseudocode,
            "discovery_score": ds,
            "benchmark_score":   float(parsed.get("benchmark_score", 0)),
            "transfer_score":    float(parsed.get("transfer_score", 0)),
            "stability_score":   float(parsed.get("stability_score", 0)),
            "sample_efficiency": float(parsed.get("sample_efficiency", 0)),
            "reasoning_depth":   float(parsed.get("reasoning_depth", 0)),
            "complexity_penalty":float(parsed.get("complexity_penalty", 1)),
            "compute_cost":      float(parsed.get("compute_cost", 1)),
            "inference_latency": float(parsed.get("inference_latency", 1)),
            "failure_modes": critic_failures, "mitigation": mitigation,
            "next_direction": next_direction, "mutation_applied": mutation_applied,
            "parent_arch_id": champion["arch_id"] if champion else None,
            "insight_for_core": insight_for_core, "research_branch": "main",
        }, on_conflict="arch_id")
        print(f"[RARL] rarl_architectures: {arch_id} role={role}")
    except Exception as e:
        print(f"[RARL] rarl_architectures error (non-fatal): {e}")

    # Step 8: write rarl_epochs log
    try:
        sb_post("rarl_epochs", {
            "epoch_number": epoch_number, "research_goal": research_goal[:300],
            "research_domain": research_domain,
            "champion_before": champion["arch_id"] if champion else None,
            "champion_after": arch_id if beats_champion else (champion["arch_id"] if champion else None),
            "ds_before": ds_before, "ds_after": ds if beats_champion else ds_before,
            "ds_improvement": (ds - ds_before) if beats_champion else 0.0,
            "new_champion": beats_champion,
            "agents_active": ["Planner","Literature","Theory","Architect","Experiment",
                              "Critic","Evaluation","Archivist","Meta-Learning","Prompt Evolution"],
            "insights_count": 1, "branch": "main",
            "groq_model_used": GROQ_MODEL, "duration_seconds": duration,
        })
        print(f"[RARL] rarl_epochs: epoch={epoch_number}")
    except Exception as e:
        print(f"[RARL] rarl_epochs error (non-fatal): {e}")

    # Step 9: hot_reflection -- main pipeline integration point
    quality  = round(min(1.0, ds / 3.0), 3) if ds > 0 else 0.4
    patterns = [p for p in [
        core_mechanism[:120] if core_mechanism else None,
        insight_for_core[:120] if insight_for_core else None,
        next_direction[:120] if next_direction else None,
        compressed[:120] if compressed else None,
        parsed.get("meta_learning_note", "")[:120] or None,
    ] if p]
    ok = sb_post("hot_reflections", {
        "task_summary": f"RARL Epoch {epoch_number} [{research_domain}]: {research_goal[:150]}",
        "domain": "rarl", "new_patterns": patterns[:5],
        "new_mistakes": [f[:120] for f in critic_failures[:3]],
        "quality_score": quality,
        "gaps_identified": [next_direction] if next_direction else None,
        "reflection_text": (
            f"Arch: {arch_id} | DS: {ds:.3f} | Role: {role} | "
            f"Mutation: {mutation_applied} | Duration: {duration}s | "
            f"For CORE: {insight_for_core[:150]}"
        ),
        "processed_by_cold": 0, "source": "rarl",
    })
    print(f"[RARL] hot_reflection ok={ok} quality={quality}")

    # Step 10: critic failures -> mistakes
    for failure in critic_failures[:3]:
        if failure and len(failure) > 5:
            try:
                sb_post("mistakes", {
                    "domain": "rarl", "context": f"Epoch {epoch_number}: {arch_id}",
                    "what_failed": failure[:300], "correct_approach": mitigation[:300],
                    "root_cause": failure[:200], "how_to_avoid": mitigation[:200], "severity": "medium",
                })
            except Exception as e:
                print(f"[RARL] mistake write error (non-fatal): {e}")

    # Step 11: compressed insight -> knowledge_base (upsert to avoid 409 on duplicate arch_id)
    if compressed and len(compressed) > 10:
        try:
            sb_upsert("knowledge_base", {
                "domain": "rarl", "topic": arch_id, "instruction": compressed,
                "content": core_mechanism[:500], "confidence": "medium", "source_type": "evolved",
            }, on_conflict="domain,topic")
        except Exception as e:
            print(f"[RARL] knowledge_base write error (non-fatal): {e}")

    # Step 12: significant discovery -> evolution_queue + Telegram
    ds_improvement = ds - ds_before
    if beats_champion and ds_improvement > 0.3:
        try:
            sb_post("evolution_queue", {
                "change_type": "rarl_discovery",
                "change_summary": (
                    f"[P2] RARL Champion: {arch_id} | "
                    f"DS: {ds_before:.2f} -> {ds:.2f} (+{ds_improvement:.2f}) | {compressed[:150]}"
                ),
                "confidence": round(quality, 3),
                "pattern_key": f"rarl_epoch_{epoch_number}",
                "diff_content": json.dumps({
                    "arch_id": arch_id, "hypothesis": hypothesis[:300],
                    "core_mechanism": core_mechanism[:300],
                    "insight_for_core": insight_for_core[:200], "discovery_score": ds,
                }, indent=2),
                "status": "pending",
            })
            notify(
                f"RARL New Champion\nEpoch {epoch_number} | {research_domain}\n"
                f"Arch: {arch_id}\nDS: {ds_before:.2f} -> {ds:.2f} (+{ds_improvement:.2f})\n"
                f"For CORE: {insight_for_core[:150]}\nNext: {next_direction[:100]}"
            )
        except Exception as e:
            print(f"[RARL] evolution_queue error (non-fatal): {e}")

    print(f"[RARL] Epoch {epoch_number} done. DS={ds:.3f} role={role} ok={ok} t={duration}s")
    return ok


# -- Public source ingestion ---------------------------------------------------
def _ingest_public_sources() -> str:
    sources = [
        "https://raw.githubusercontent.com/langchain-ai/langchain/master/README.md",
        "https://raw.githubusercontent.com/openai/openai-cookbook/main/README.md",
        "https://raw.githubusercontent.com/anthropics/anthropic-sdk-python/main/README.md",
        "https://raw.githubusercontent.com/tiangolo/fastapi/master/README.md",
        "https://raw.githubusercontent.com/crewAIInc/crewAI/main/README.md",
    ]
    hour_slot = int(time.time() // 3600) % len(sources)
    to_fetch = [sources[hour_slot], sources[(hour_slot + 1) % len(sources)]]
    combined = []
    headers = {"User-Agent": "CORE-AGI/6.0"}
    for url in to_fetch:
        try:
            r = httpx.get(url, timeout=8, follow_redirects=True, headers=headers)
            if r.status_code == 200:
                text = r.text.strip()[:2000]
                source_name = url.split("/")[4]
                combined.append(f"[{source_name}]\n{text}")
        except Exception:
            pass
    return "\n\n".join(combined)


# -- Background researcher -----------------------------------------------------
_last_smap_update: float = 0.0
_SMAP_UPDATE_INTERVAL = 21600  # 6 hours -- sync system_map tool_count + reconcile

def background_researcher():
    global _last_research_run, _last_public_source_run, _last_smap_update
    print("[RESEARCH] background researcher started - real signal + simulation + public source mode")
    _cycle_count = 0

  # Restore timestamps from Supabase to survive redeploys
    global _last_research_run, _last_public_source_run
    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_research_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_research_ts:")[-1].strip().split()[0]
            _last_research_run = float(val)
            print(f"[RESEARCH] Restored last_research_ts: {datetime.utcfromtimestamp(_last_research_run).isoformat()}")
        else:
            _last_research_run = time.time() - (_IMPROVEMENT_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore research_ts error: {e}")
        _last_research_run = time.time() - (_IMPROVEMENT_INTERVAL + 60)

    try:
        rows = sb_get("sessions", "select=summary&summary=like.*last_public_source_ts*&order=created_at.desc&limit=1", svc=True)
        if rows:
            val = rows[0]["summary"].split("last_public_source_ts:")[-1].strip().split()[0]
            _last_public_source_run = float(val)
            print(f"[RESEARCH] Restored last_public_source_ts: {datetime.utcfromtimestamp(_last_public_source_run).isoformat()}")
        else:
            _last_public_source_run = time.time() - (_PUBLIC_SOURCE_INTERVAL + 60)
    except Exception as e:
        print(f"[RESEARCH] restore public_source_ts error: {e}")
        _last_public_source_run = time.time() - (_PUBLIC_SOURCE_INTERVAL + 60)

    while True:
        try:
            now = time.time()
            if now - _last_research_run >= _IMPROVEMENT_INTERVAL:
                print("[RESEARCH] Running signal extraction cycle...")
                _last_research_run = now
                sb_post("sessions", {"summary": f"[state_update] last_research_ts: {now}", "actions": ["last_research_ts persisted"], "interface": "mcp"})
                _cycle_count += 1

                public_content = ""
                if now - _last_public_source_run >= _PUBLIC_SOURCE_INTERVAL:
                    print("[RESEARCH] Fetching public sources...")
                    public_content = _ingest_public_sources()
                    if public_content:
                        _last_public_source_run = now
                        sb_post("sessions", {"summary": f"[state_update] last_public_source_ts: {now}", "actions": ["last_public_source_ts persisted"], "interface": "mcp"})
                        print(f"[RESEARCH] Public sources fetched: {len(public_content)} chars")
                    else:
                        print("[RESEARCH] Public sources returned empty - skipping")

                real_ok = _extract_real_signal()
                time.sleep(3)
                sim_ok = _run_rarl_epoch()

                try:
                    groq_pending = sb_get(
                        "evolution_queue",
                        "select=id,change_type,change_summary,diff_content,confidence,pattern_key&status=eq.pending&order=id.asc&limit=20",
                        svc=True
                    )
                    auto_applied = 0
                    for evo in groq_pending:
                        ctype = evo.get("change_type", "")
                        conf  = float(evo.get("confidence") or 0)
                        if ctype == "knowledge" and conf >= KNOWLEDGE_AUTO_CONFIDENCE:
                            r = apply_evolution(evo["id"])
                            if r.get("ok"):
                                auto_applied += 1
                            time.sleep(1)
                    if auto_applied:
                        print(f"[RESEARCH] Auto-applied {auto_applied} knowledge evolutions (conf>={KNOWLEDGE_AUTO_CONFIDENCE})")
                except Exception as _ae:
                    print(f"[RESEARCH] auto-apply error: {_ae}")

                try:
                    if real_ok or sim_ok or auto_applied:
                        parts = []
                        if real_ok:
                            parts.append("new patterns extracted")
                        if sim_ok:
                            parts.append("rarl epoch complete")
                        if auto_applied:
                            parts.append(f"{auto_applied} evolutions auto-applied")
                        if public_content:
                            parts.append("public sources ingested")
                        notify(f"[CORE] Researcher cycle #{_cycle_count}\n" + " | ".join(parts))
                except Exception:
                    pass

            # system_map periodic auto-update (every 6h)
            try:
                if time.time() - _last_smap_update >= _SMAP_UPDATE_INTERVAL:
                    from core_tools import t_system_map_scan
                    result = t_system_map_scan(trigger="session_end")
                    _last_smap_update = time.time()
                    ins = result.get("inserted_tools", [])
                    tomb = result.get("tombstoned_tools", [])
                    upd = result.get("updates_applied", 0)
                    print(f"[SMAP] Auto-update: {upd} volatile updates, {len(ins)} inserted, {len(tomb)} tombstoned")
                    if ins or tomb:
                        notify(f"[SMAP] system_map auto-reconciled\nInserted: {ins}\nTombstoned: {tomb}")
            except Exception as _sme:
                print(f"[SMAP] auto-update error: {_sme}")

        except Exception as e:
            print(f"[RESEARCH] loop error: {e}")
        time.sleep(60)


# -- Pattern semantic clustering -----------------------------------------------
def _groq_cluster_patterns(batch_counts: "Counter", batch_domain: dict, batch_sources: dict) -> tuple:
    if len(batch_counts) <= 1:
        return batch_counts, batch_domain, batch_sources
    try:
        raw_keys = list(batch_counts.keys())
        keys_text = "\n".join(f"  {i+1}. {k[:180]}" for i, k in enumerate(raw_keys))
        prompt = (
            "You are CORE's pattern deduplicator.\n"
            "Below is a list of behavioral patterns extracted from recent sessions.\n"
            "Many express the SAME rule with different wording.\n\n"
            f"Patterns:\n{keys_text}\n\n"
            "Task: Group semantically identical or near-identical patterns together.\n"
            "For each group, choose the BEST canonical form (most specific, most actionable).\n"
            "Return ONLY valid JSON -- a flat object mapping each original pattern number "
            "to its canonical pattern string. All patterns must appear. "
            "Patterns that are truly unique map to themselves.\n"
            "Example: {\"1\": \"canonical text\", \"2\": \"canonical text\", \"3\": \"different rule\"}\n"
            "No preamble. No markdown. Pure JSON only."
        )
        raw = gemini_chat(system="You are CORE's pattern clustering engine. Return only valid JSON.", user=prompt, max_tokens=2000, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        mapping_by_num = json.loads(raw)

        key_map: dict = {}
        for i, raw_key in enumerate(raw_keys):
            canonical = mapping_by_num.get(str(i + 1), raw_key).strip()
            if not canonical or len(canonical) < 5:
                canonical = raw_key
            key_map[raw_key] = canonical[:500]

        new_counts: Counter = Counter()
        new_domain: dict = {}
        new_sources: dict = {}
        for raw_key, count in batch_counts.items():
            canonical = key_map.get(raw_key, raw_key)
            new_counts[canonical] += count
            new_domain.setdefault(canonical, batch_domain.get(raw_key, "general"))
            existing_src = new_sources.get(canonical, set())
            new_sources[canonical] = existing_src | batch_sources.get(raw_key, {"real"})

        merged_count = len(batch_counts) - len(new_counts)
        print(f"[COLD] Clustering: {len(batch_counts)} raw -> {len(new_counts)} canonical ({merged_count} merged)")
        return new_counts, new_domain, new_sources

    except Exception as e:
        print(f"[COLD] Clustering failed (non-fatal, using raw keys): {e}")
        return batch_counts, batch_domain, batch_sources


# -- Listen Mode ---------------------------------------------------------------
def listen_stream():
    import time as _time
    deadline = _time.time() + 3600
    dry_cycles = 0
    cycle = 0

    while _time.time() < deadline:
        cycle += 1

        try:
            result = run_cold_processor()
            patterns_found = result.get("patterns_found", 0) if isinstance(result, dict) else 0
            evos_queued    = result.get("evolutions_queued", 0) if isinstance(result, dict) else 0
            yield json.dumps({
                "type": "cold_run",
                "cycle": cycle,
                "patterns_found": patterns_found,
                "evolutions_queued": evos_queued,
                "ts": datetime.utcnow().isoformat(),
            }) + "\n"
            if patterns_found == 0:
                dry_cycles += 1
            else:
                dry_cycles = 0
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower():
                yield json.dumps({"type": "stop", "reason": "groq_limit", "cycle": cycle}) + "\n"
                return
            yield json.dumps({"type": "cold_error", "error": err[:200], "cycle": cycle}) + "\n"

        try:
            evos = sb_get(
                "evolution_queue",
                "select=id,change_type,change_summary,confidence,pattern_key,domain"
                " &status=eq.pending&order=confidence.desc&limit=50",
                svc=True
            ) or []
            yield json.dumps({
                "type": "evolutions",
                "cycle": cycle,
                "count": len(evos),
                "items": evos[:20],
            }) + "\n"
        except Exception as e:
            yield json.dumps({"type": "evo_error", "error": str(e)[:200], "cycle": cycle}) + "\n"

        try:
            pats = sb_get(
                "pattern_frequency",
                "select=pattern_key,frequency,domain&stale=eq.false&order=frequency.desc&limit=20",
                svc=True
            ) or []
            yield json.dumps({
                "type": "patterns",
                "cycle": cycle,
                "count": len(pats),
                "items": pats,
            }) + "\n"
        except Exception as e:
            yield json.dumps({"type": "pat_error", "error": str(e)[:200], "cycle": cycle}) + "\n"

        try:
            hot_gaps = sb_get(
                "hot_reflections",
                "select=domain,quality_score,gaps_identified"
                "&processed_by_cold=eq.0&id=gt.1&quality_score=gte.0.5"
                "&order=created_at.desc&limit=10",
                svc=True
            ) or []
            yield json.dumps({
                "type": "hot_gaps",
                "cycle": cycle,
                "unprocessed": len(hot_gaps),
                "items": [
                    {"domain": h.get("domain"), "quality": h.get("quality_score"),
                     "gaps": h.get("gaps_identified")}
                    for h in hot_gaps
                ],
            }) + "\n"
        except Exception as e:
            yield json.dumps({"type": "hot_error", "error": str(e)[:200], "cycle": cycle}) + "\n"

        if dry_cycles >= 2:
            yield json.dumps({"type": "stop", "reason": "drained", "cycle": cycle}) + "\n"
            return

        _time.sleep(60)

    yield json.dumps({"type": "stop", "reason": "timeout", "cycle": cycle}) + "\n"


# ── TASK-4: Binance Price Monitor ─────────────────────────────────────────────

def _fetch_price(symbol: str):
    """Fetch current Binance price for symbol. Returns None on error."""
    try:
        r = httpx.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=8,
        )
        data = r.json()
        return float(data["price"]) if "price" in data else None
    except Exception as e:
        print(f"[PRICE] fetch error {symbol}: {e}")
        return None


def price_monitor_loop():
    """
    Background price monitoring thread.
    Polls Binance every BINANCE_MONITOR_INTERVAL_S seconds.
    Sends Telegram alert when price moves > BINANCE_ALERT_THRESHOLD_PCT%.
    Stores price alerts in Supabase market_signals table.
    """
    global _price_monitor_running, _price_monitor_last_prices
    _price_monitor_running = True
    print(f"[PRICE] monitor started — symbols={_PRICE_MONITOR_SYMBOLS} threshold={_PRICE_ALERT_THRESHOLD}% interval={_PRICE_MONITOR_INTERVAL}s")

    while _price_monitor_running:
        for symbol in _PRICE_MONITOR_SYMBOLS:
            symbol = symbol.strip().upper()
            if not symbol:
                continue
            current = _fetch_price(symbol)
            if current is None:
                continue

            last = _price_monitor_last_prices.get(symbol)
            if last is not None:
                change_pct = ((current - last) / last) * 100
                if abs(change_pct) >= _PRICE_ALERT_THRESHOLD:
                    direction = "UP" if change_pct > 0 else "DOWN"
                    msg = (
                        f"\U0001f6a8 PRICE ALERT: {symbol} {direction} {change_pct:+.2f}%\n"
                        f"Current: {current:,.4f} USDT\n"
                        f"Previous: {last:,.4f} USDT\n"
                        f"Change: {current - last:+,.4f} USDT\n\n"
                        f"Reply to trade:\n"
                        f"  APPROVE {symbol} <qty>\n"
                        f"  SELL {symbol} <qty>\n"
                        f"  REJECT"
                    )
                    print(f"[PRICE] {msg}")
                    try:
                        notify(msg)
                    except Exception as ne:
                        print(f"[PRICE] notify error: {ne}")
                    try:
                        sb_post_critical("market_signals", {
                            "signal_type": "price_alert",
                            "token_symbol": symbol,
                            "chain": "CEX",
                            "data": {
                                "price": current,
                                "previous": last,
                                "change_pct": round(change_pct, 4),
                                "direction": direction,
                            },
                        })
                    except Exception as se:
                        print(f"[PRICE] supabase log error: {se}")

            _price_monitor_last_prices[symbol] = current
            print(f"[PRICE] {symbol} = {current:,.4f} USDT")

        _threading.Event().wait(_PRICE_MONITOR_INTERVAL)

    print("[PRICE] monitor stopped")


def start_price_monitor():
    """Start price monitor in background thread. Called from startup."""
    t = _threading.Thread(target=price_monitor_loop, daemon=True, name="price_monitor")
    t.start()
    return t


# -- AGI-01: Weekly Cross-Domain Synthesis ------------------------------------

def _run_cross_domain_synthesis():
    """AGI-01: Cross-domain pattern synthesis. Runs weekly on Wednesday.
    Reads top 5 patterns per domain, uses Groq to find structural similarities,
    writes unified insights to knowledge_base(domain=synthesis).
    Notifies owner via Telegram.
    """
    global _last_synthesis_run
    print("[SYNTH] Starting cross-domain synthesis...")

    # Step 1: Load top patterns per domain
    try:
        top_patterns = sb_get(
            "pattern_frequency",
            "select=pattern_key,domain,frequency&stale=eq.false&order=frequency.desc&limit=200",
            svc=True
        ) or []
    except Exception as e:
        print(f"[SYNTH] pattern load error: {e}")
        _last_synthesis_run = time.time()
        return

    if not top_patterns:
        print("[SYNTH] No patterns found -- skipping")
        _last_synthesis_run = time.time()
        return

    # Group top 5 per domain
    domain_patterns: dict = {}
    for p in top_patterns:
        d = p.get("domain", "general")
        if d not in domain_patterns:
            domain_patterns[d] = []
        if len(domain_patterns[d]) < 5:
            domain_patterns[d].append(p.get("pattern_key", ""))

    if len(domain_patterns) < 2:
        print(f"[SYNTH] Only {len(domain_patterns)} domain(s) -- need 2+ for cross-domain synthesis")
        _last_synthesis_run = time.time()
        return

    print(f"[SYNTH] Synthesizing across {len(domain_patterns)} domains: {list(domain_patterns.keys())[:8]}")

    # Step 2: Ask Groq to find structural similarities
    domain_summary = "\n".join(
        f"Domain '{d}': " + " | ".join(ps[:3])
        for d, ps in list(domain_patterns.items())[:10]
    )
    system_prompt = (
        "You are an expert at finding structural patterns across different knowledge domains. "
        "Given patterns from multiple domains, identify which patterns share the same ROOT CAUSE structure "
        "even if they appear in different contexts. Focus on actionable insights."
    )
    user_prompt = (
        f"Analyze these patterns from CORE AGI's operational domains:\n\n{domain_summary}\n\n"
        "Find 3-5 cross-domain insights where the same root cause appears in multiple domains. "
        "For each insight, write: INSIGHT: <one sentence>. DOMAINS: <which domains>. UNIFIED_RULE: <actionable rule that applies across all those domains>. "
        "Be specific and actionable. Format as JSON array: [{\"insight\": str, \"domains\": [str], \"unified_rule\": str, \"confidence\": 0.0-1.0}]"
    )

    try:
        raw = gemini_chat(system_prompt, user_prompt, max_tokens=2048, json_mode=True)
        print(f"[SYNTH] Gemini raw length={len(raw)} first_100={raw[:100]}")
        # json_mode=True guarantees JSON -- parse directly, strip fences as fallback
        import re as _re_s
        clean = _re_s.sub(r'```(?:json)?\s*|\s*```', '', raw).strip()
        # Try direct parse first, then array extraction as fallback
        try:
            insights = json.loads(clean)
            if isinstance(insights, dict):  # wrapped in {insights: [...]}
                insights = insights.get("insights", list(insights.values())[0] if insights else [])
        except json.JSONDecodeError:
            json_match = _re_s.search(r'\[.*\]', clean, _re_s.DOTALL)
            if not json_match:
                print(f"[SYNTH] No JSON parseable in response: {clean[:500]}")
                _last_synthesis_run = time.time()
                return
            insights = json.loads(json_match.group(0))
    except Exception as e:
        print(f"[SYNTH] Gemini synthesis error: {e} | raw: {raw[:200] if 'raw' in dir() else 'N/A'}")
        _last_synthesis_run = time.time()
        return

    if not insights:
        print("[SYNTH] No insights generated")
        _last_synthesis_run = time.time()
        return

    # Step 3: Write to knowledge_base + hot_reflections
    written = 0
    try:
        import hashlib as _hl
        date_tag = datetime.utcnow().strftime("%Y%m%d")
        for ins in insights:
            insight_text = ins.get("insight", "")
            unified_rule = ins.get("unified_rule", "")
            domains = ins.get("domains", [])
            conf_raw = ins.get("confidence", 0.7)
            if not insight_text or not unified_rule:
                continue
            # Map float confidence to enum
            conf_val = float(conf_raw) if isinstance(conf_raw, (int, float)) else 0.7
            conf_enum = "proven" if conf_val >= 0.9 else "high" if conf_val >= 0.75 else "medium" if conf_val >= 0.5 else "low"
            topic_hash = _hl.md5(insight_text[:80].encode()).hexdigest()[:8]
            topic = f"cross_domain_{topic_hash}_{date_tag}"
            content = (
                f"INSIGHT: {insight_text}\n"
                f"DOMAINS: {', '.join(domains)}\n"
                f"UNIFIED_RULE: {unified_rule}\n"
                f"GENERATED: {datetime.utcnow().isoformat()}\n"
                f"SOURCE: AGI-01 cross-domain synthesis"
            )
            # Upsert to avoid duplicates on re-run
            existing = sb_get(
                "knowledge_base",
                f"select=id&domain=eq.synthesis&topic=eq.{topic}&id=gt.1",
                svc=True
            )
            if existing:
                print(f"[SYNTH] Skipped duplicate insight: {topic}")
                continue
            ok = sb_post("knowledge_base", {
                "domain": "synthesis",
                "topic": topic,
                "content": content,
                "confidence": conf_enum,
                "source_type": "evolved",
                "instruction": f"Apply this unified rule across domains: {', '.join(domains)}",
            })
            if ok:
                written += 1
                print(f"[SYNTH] Insight written: {insight_text[:80]}")
    except Exception as e:
        print(f"[SYNTH] KB write error: {e}")

    # Step 4: Hot reflection for cold processor
    try:
        if written > 0:
            patterns_list = [ins.get("insight", "") for ins in insights if ins.get("insight")]
            sb_post("hot_reflections", {
                "domain": "synthesis",
                "task_summary": f"AGI-01 cross-domain synthesis: {written} insights across {len(domain_patterns)} domains",
                "quality_score": 0.85,
                "new_patterns": json.dumps(patterns_list),
                "processed_by_cold": False,
                "source": "real",
                "reflection_text": f"Cross-domain synthesis identified {written} structural similarities. Domains covered: {list(domain_patterns.keys())}",
            })
    except Exception as e:
        print(f"[SYNTH] hot_reflection error: {e}")

    _last_synthesis_run = time.time()

    # Step 5: Notify owner
    try:
        if written > 0:
            summary_lines = [f"  {i+1}. {ins.get('insight','')[:80]}" for i, ins in enumerate(insights[:5])]
            notify(
                f"[SYNTH] Weekly cross-domain synthesis complete.\n"
                f"{written} insight(s) written to KB (domain=synthesis):\n"
                + "\n".join(summary_lines)
            )
        else:
            notify("[SYNTH] Weekly synthesis ran but produced no new insights.")
        print(f"[SYNTH] Done. {written} insights written.")
    except Exception as e:
        print(f"[SYNTH] notify error: {e}")


# -- AGI-02: Nightly Self-Diagnosis -------------------------------------------

def _run_self_diagnosis():
    """AGI-02: Autonomous gap detection. Runs nightly at 02:00 WIB.
    Analyzes: mistakes, quality trend, KB domain coverage, stale tasks.
    Generates self_assigned tasks for identified gaps.
    Notifies owner via Telegram for approval before execution.
    """
    global _last_self_diagnosis_run
    print("[DIAG] Starting nightly self-diagnosis...")
    gaps = []

    # Analysis 1: Top structural weaknesses from recent mistakes
    try:
        recent_mistakes = sb_get(
            "mistakes",
            "select=domain,root_cause,severity&order=created_at.desc&limit=50&id=gt.1",
            svc=True
        ) or []
        domain_counts = Counter(m.get("domain", "general") for m in recent_mistakes)
        high_sev = [m for m in recent_mistakes if m.get("severity") in ("high", "critical")]
        high_sev_domains = Counter(m.get("domain", "general") for m in high_sev)
        top_weak = high_sev_domains.most_common(3)
        for domain, count in top_weak:
            if count >= 2:
                gaps.append({
                    "source": "mistake_cluster",
                    "title": f"Investigate recurring {domain} failures",
                    "description": f"Self-diagnosis: {count} high/critical severity mistakes in domain '{domain}' in last 50 sessions. Root causes need structural fix.",
                    "priority": 4,
                })
        print(f"[DIAG] Mistakes: {len(recent_mistakes)} scanned, {len(top_weak)} weak domains found")
    except Exception as e:
        print(f"[DIAG] mistake analysis error: {e}")

    # Analysis 2: Quality trend — flag if declining
    try:
        metrics = sb_get(
            "quality_metrics",
            "select=quality_score,created_at&order=created_at.desc&limit=10&id=gt.1",
            svc=True
        ) or []
        if len(metrics) >= 5:
            scores = [float(m.get("quality_score") or 0) for m in metrics]
            recent_avg = sum(scores[:5]) / 5
            older_avg = sum(scores[5:]) / max(len(scores[5:]), 1)
            if recent_avg < 0.75 or (older_avg > 0 and recent_avg < older_avg - 0.05):
                gaps.append({
                    "source": "quality_decline",
                    "title": "Investigate quality score decline",
                    "description": f"Self-diagnosis: recent 5-session avg quality={recent_avg:.2f}, prior avg={older_avg:.2f}. Quality declining or below threshold 0.75. Needs root cause analysis.",
                    "priority": 4,
                })
        print(f"[DIAG] Quality: {len(metrics)} sessions scanned, recent_avg={sum(scores[:5])/5 if len(scores)>=5 else 'N/A'}")
    except Exception as e:
        print(f"[DIAG] quality analysis error: {e}")

    # Analysis 3: KB domain coverage — flag domains with <10 entries
    try:
        kb_rows = sb_get(
            "knowledge_base",
            "select=domain&id=gt.1&active=eq.true",
            svc=True
        ) or []
        domain_kb_counts = Counter(r.get("domain", "general") for r in kb_rows)
        shallow = [(d, c) for d, c in domain_kb_counts.items() if c < 10 and not d.startswith("project:")]
        for domain, count in shallow[:3]:  # cap at 3 gaps
            gaps.append({
                "source": "kb_coverage",
                "title": f"Expand KB coverage for domain: {domain}",
                "description": f"Self-diagnosis: domain '{domain}' has only {count} KB entries. Knowledge base is shallow here. Consider ingesting or adding structured entries.",
                "priority": 3,
            })
        print(f"[DIAG] KB: {len(domain_kb_counts)} domains, {len(shallow)} shallow (<10 entries)")
    except Exception as e:
        print(f"[DIAG] KB coverage analysis error: {e}")

    # Analysis 4: Stale tasks — pending >14 days with no checkpoint
    try:
        stale_cutoff = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_tasks = sb_get(
            "task_queue",
            f"select=id,task,priority&status=eq.pending&created_at=lt.{stale_cutoff}&source=neq.self_assigned",
            svc=True
        ) or []
        if len(stale_tasks) >= 3:
            gaps.append({
                "source": "stale_tasks",
                "title": "Review stale pending tasks (>14 days)",
                "description": f"Self-diagnosis: {len(stale_tasks)} tasks have been pending >14 days with no progress. Review for blockers, deprioritization, or cancellation.",
                "priority": 3,
            })
        print(f"[DIAG] Stale tasks: {len(stale_tasks)} found")
    except Exception as e:
        print(f"[DIAG] stale task analysis error: {e}")

    # Generate self-assigned tasks -- with dedup guard before each insert
    tasks_created = []
    try:
        existing_pending = sb_get(
            "task_queue",
            "select=task&status=in.(pending,in_progress)&source=eq.self_assigned&limit=200",
            svc=True
        ) or []
        existing_titles = set()
        for row in existing_pending:
            try:
                t = json.loads(row.get("task") or "{}")
                existing_titles.add(t.get("title", "").strip().lower())
            except Exception:
                pass
    except Exception as e:
        print(f"[DIAG] dedup pre-fetch error: {e}")
        existing_titles = set()

    for gap in gaps:
        try:
            title_key = gap["title"].strip().lower()
            if title_key in existing_titles:
                print(f"[DIAG] Skipped duplicate self-assigned task: {gap['title']}")
                continue
            task_payload = {
                "task": json.dumps({
                    "title": gap["title"],
                    "description": gap["description"],
                    "source": "self_assigned",
                }),
                "status": "pending",
                "priority": gap["priority"],
                "source": "self_assigned",
            }
            result = sb_post("task_queue", task_payload)
            if result:
                tasks_created.append(gap["title"])
                existing_titles.add(title_key)  # prevent same-run dupes if gaps list has overlap
                print(f"[DIAG] Created self-assigned task: {gap['title']}")
        except Exception as e:
            print(f"[DIAG] task creation error for '{gap['title']}': {e}")

    # Persist timestamp to Supabase so it survives Railway redeploys
    _last_self_diagnosis_run = time.time()
    try:
        sb_post("sessions", {
            "state_key": _DIAG_STATE_KEY,
            "state_value": str(_last_self_diagnosis_run),
            "summary": f"AGI-02 self-diagnosis ran at {datetime.utcnow().isoformat()} -- {len(tasks_created)} tasks created",
        })
        print(f"[DIAG] Persisted last_diag_ts to Supabase: {_last_self_diagnosis_run}")
    except Exception as e:
        print(f"[DIAG] persist timestamp error: {e}")
    try:
        if tasks_created:
            task_list = "\n".join(f"  - {t}" for t in tasks_created)
            notify(
                f"[DIAG] Nightly self-diagnosis complete.\n"
                f"{len(tasks_created)} gap(s) identified and queued (source=self_assigned, status=pending):\n"
                f"{task_list}\n\n"
                f"Review in next session before execution."
            )
        else:
            notify("[DIAG] Nightly self-diagnosis: no gaps found. All systems nominal.")
        print(f"[DIAG] Done. {len(tasks_created)} self-assigned tasks created.")
    except Exception as e:
        print(f"[DIAG] notify error: {e}")
