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
import time
from collections import Counter
from datetime import datetime, timedelta

import httpx

from core_config import (
    GROQ_MODEL, GROQ_FAST,
    SUPABASE_URL,
    COLD_HOT_THRESHOLD, COLD_TIME_THRESHOLD, COLD_KB_GROWTH_THRESHOLD,
    PATTERN_EVO_THRESHOLD, KNOWLEDGE_AUTO_CONFIDENCE,
    KB_MINE_BATCH_SIZE, KB_MINE_RATIO_THRESHOLD,
    sb_get, sb_post, sb_post_critical, sb_patch, sb_upsert, _sbh_count_svc,
    groq_chat,
)
from core_github import notify, gh_write

# Training globals
_last_cold_run: float = 0.0
_last_cold_kb_count: int = 0
_last_research_run: float = 0.0
_IMPROVEMENT_INTERVAL = 3600  # 60 min
_last_public_source_run: float = 0.0
_PUBLIC_SOURCE_INTERVAL = 21600  # 6 hours

# Source confidence multipliers (Phase 3)
_SRC_CONF = {"real": 1.0, "simulation": 0.7, "both": 1.3}


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
        for kw, d in [("supabase","db"),("github","code"),("telegram","bot"),("mcp","mcp"),("training","training"),("knowledge","kb")]:
            if kw in summary.lower(): domain = d; break
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

            # --- Enrich: pull ALL data since last hot_reflection (the anchor) ---
            # Anchor = timestamp of the PREVIOUS hot_reflection row.
            # Groq sees the full delta: mistakes, KB, tasks, changelogs -- everything
            # since the last scan, not just the current second.
            # Fallback chain: last hot_reflection -> session created_at -> 24h ago.
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
            # Strip timezone suffix so PostgREST accepts the timestamp
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
            # --- End enrichment ---

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
            raw = groq_chat(prompt, model=GROQ_FAST, max_tokens=500)
            parsed = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
            groq_patterns = [p for p in parsed.get("patterns", []) if isinstance(p, str) and len(p) > 5][:5]
            # Merge: seed patterns first, then Groq additions (deduplicated)
            seen = set(p.lower() for p in new_patterns)
            for p in groq_patterns:
                if p.lower() not in seen:
                    new_patterns.append(p)
                    seen.add(p.lower())
            if quality_score is None:
                quality_score = float(parsed.get("quality") or 0.7)
            _gaps_raw = parsed.get("gaps") or None
            # gaps_identified is text[] in DB -- must be list or null, never bare string
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
            "processed_by_cold": False,
        })
        print(f"[HOT] ok={ok} domain={domain}")
        return ok
    except Exception as e:
        print(f"[HOT] error: {e}")
        return False


# -- Cold processor ------------------------------------------------------------
def _groq_synthesize_cold(hots: list, batch_counts: Counter, batch_domain: dict) -> str:
    """Ask Groq to synthesize a meaningful cold reflection summary from the batch.
    Returns a rich summary string. Falls back to dumb counter string on failure.
    """
    fallback = f"Processed {len(hots)} hots. {len(batch_counts)} unique patterns."
    try:
        # Build top-patterns context: top 15 by frequency in this batch
        top = sorted(batch_counts.items(), key=lambda x: x[1], reverse=True)[:15]
        top_text = "\n".join(
            f"  [{batch_domain.get(k,'?')}] ({v}x) {k[:120]}"
            for k, v in top
        )
        # Domain breakdown
        domain_counts: Counter = Counter(batch_domain[k] for k in batch_counts)
        domain_text = ", ".join(f"{d}:{n}" for d, n in domain_counts.most_common(6))
        # Session summaries (task_summary field from hots)
        session_summaries = "\n".join(
            f"  - {h.get('task_summary','')[:150]}" for h in hots[:10]
        )
        prompt = (
            f"You are CORE's cold processor synthesis engine.\n"
            f"You just processed {len(hots)} hot reflections covering {len(batch_counts)} unique patterns.\n\n"
            f"Domain breakdown: {domain_text}\n\n"
            f"Session summaries:\n{session_summaries}\n\n"
            f"Top patterns by frequency:\n{top_text}\n\n"
            f"Write a 3-5 sentence synthesis of this batch. Cover:\n"
            f"1. What themes/domains dominated\n"
            f"2. Most important recurring patterns (name them)\n"
            f"3. Any gaps or risks identified\n"
            f"4. Overall system health signal\n"
            f"Be specific and actionable. No preamble. Plain text only."
        )
        synthesis = groq_chat(prompt, model=GROQ_MODEL, max_tokens=400)
        synthesis = synthesis.strip()
        if len(synthesis) > 50:
            return synthesis
        return fallback
    except Exception as e:
        print(f"[COLD] synthesis failed (non-fatal): {e}")
        return fallback


def _groq_kb_content(pattern_key: str, domain: str, frequency: int, src_key: str) -> str:
    """Ask Groq to write proper KB entry content for a pattern that hit threshold.
    Returns a rich content string. Falls back to the raw pattern key on failure.
    """
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
        content = groq_chat(prompt, model=GROQ_FAST, max_tokens=350)
        content = content.strip()
        if len(content) > 30:
            return content
        return pattern_key
    except Exception as e:
        print(f"[COLD] kb_content generation failed (non-fatal): {e}")
        return pattern_key


def run_cold_processor():
    try:
        hots = sb_get("hot_reflections",
                      "select=id,domain,new_patterns,new_mistakes,quality_score,source,task_summary&processed_by_cold=eq.0&id=gt.1&order=created_at.asc",
                      svc=True)
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
                    key = str(p)[:500]  # raised from 200 -- long patterns need full key for identity
                    batch_counts[key] += 1
                    batch_domain.setdefault(key, h.get("domain", "general"))
                    batch_sources.setdefault(key, set()).add(src)

        # --- Semantic clustering: merge near-identical patterns before counting ---
        # Fixes fragmentation bug: same concept with different wording = separate keys
        # that never accumulate past threshold. Groq clusters them into canonical keys.
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

            # ALLOWED_EVO_TYPES: cold processor only emits knowledge/code/config — never backlog
            # auto_applied fix: re-queue at milestone frequencies (10, 25, 50) so high-frequency
            # patterns get re-evaluated as KB entries, not permanently silenced after first queue.
            _milestones = {10, 25, 50, 100}
            _already_applied = (existing or {}).get("auto_applied", False)
            _at_milestone = total_freq in _milestones
            _should_queue = total_freq >= PATTERN_EVO_THRESHOLD and (
                not _already_applied or _at_milestone
            )
            if _should_queue:
                base_conf  = min(0.5 + total_freq * 0.05, 0.95)
                final_conf = round(base_conf * src_mult, 3)

                # Groq writes proper KB content for the evolution instead of raw pattern string
                kb_content = _groq_kb_content(key, domain, total_freq, src_key)

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
                    # TASK-17: auto-apply gate -- knowledge + confidence>=0.65 + source=real
                    # code/config/new_tool always require owner review
                    if final_conf >= 0.65 and src_key == "real":
                        new_evo = sb_get("evolution_queue",
                            f"select=id&pattern_key=eq.{key[:100]}&status=eq.pending&order=id.desc&limit=1",
                            svc=True)
                        if new_evo:
                            result = apply_evolution(new_evo[0]["id"])
                            if result.get("ok"):
                                auto_applied_count += 1
                                print(f"[COLD] Auto-applied evolution #{new_evo[0]['id']}: {key[:80]}")

        # Groq synthesizes a meaningful cold reflection summary
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
            # change_summary is now Groq-generated KB content — use it directly as content
            applied = sb_post_critical("knowledge_base", {
                "domain": evo.get("impact", "general"),
                "topic": pattern_key[:100] or change_summary[:100],
                "content": change_summary,  # rich Groq-written content
                "confidence": "high" if confidence >= 0.8 else "medium",
                "tags": ["evolution", "auto"], "source": "evolution_queue",
            })
            note = "Added to knowledge_base"

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
            # RETIRED 2026-03-14 (Task 7.2) — backlog is owner decision, never Groq's.
            reject_evolution(evolution_id, reason="backlog change_type retired — owner decides backlog", silent=True)
            return {"ok": False, "evolution_id": evolution_id, "change_type": change_type,
                    "note": "backlog change_type retired — auto-rejected"}

        if applied:
            sb_patch("evolution_queue", f"id=eq.{evolution_id}",
                     {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
            notify(f"Evolution #{evolution_id} applied\nType: {change_type}\n{note}")
            # BACKLOG.md deleted in Task 1.8 - backlog lives in Supabase only
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
    """Reject a single evolution. silent=True skips Telegram notify + mistakes write (use for bulk ops)."""
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
    """Bulk reject evolutions by change_type or explicit id list.
    Silent by default — one summary Telegram notify at the end.
    include_synthesized: if True, also targets status=synthesized items (not just pending).
    """
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
    """Mark patterns not seen in 30+ days as stale=true.
    Returns count of newly staled patterns."""
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
                sb_patch("pattern_frequency", f"id=eq.{row['id']}",
                         {"stale": True})
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
def cold_processor_loop():
    global _last_cold_run, _last_cold_kb_count, _last_stale_check
    print("[COLD] Background loop started")
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
                # BACKLOG.md deleted in Task 1.8 - backlog lives in Supabase only

            for evo in sb_get("evolution_queue",
                               "select=id,confidence,change_type&status=eq.pending&change_type=eq.knowledge&id=gt.1",
                               svc=True):
                conf = float(evo.get("confidence") or 0)
                if conf >= KNOWLEDGE_AUTO_CONFIDENCE:
                    apply_evolution(evo["id"])
            # TASK-9.D: stale pattern check once per 24h
            if time.time() - _last_stale_check >= _STALE_CHECK_INTERVAL:
                _check_stale_patterns()
                _last_stale_check = time.time()
        except Exception as e:
            print(f"[COLD] loop error: {e}")
        time.sleep(1800)


# -- Backlog helpers -----------------------------------------------------------
def _backlog_add(items: list) -> list:
    """Write new backlog items to Supabase backlog table."""
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
        # NOTE: Backlog items are NEVER pushed to evolution_queue.
    return new_items


def _sync_backlog_status():
    """No-op: backlog status is managed directly in the backlog table."""
    return 0


def _backlog_to_markdown() -> str:
    """DEPRECATED - BACKLOG.md deleted in Task 1.8. Backlog lives in Supabase only.
    Kept as no-op to avoid breaking any callers."""
    return "# BACKLOG.md deprecated - use get_backlog MCP tool or Supabase directly."


# -- KB Mining -----------------------------------------------------------------
def run_kb_mining(max_batches: int = 50, force: bool = False) -> dict:
    """DEPRECATED 2026-03-14 - backlog table dropped. KB mining replaced by cold processor pipeline."""
    print("[KB MINE] deprecated - backlog table dropped, no-op")
    return {"ok": False, "deprecated": True, "reason": "backlog table dropped - use evolution_queue pipeline instead"}


# -- Real signal + simulation --------------------------------------------------
def _extract_real_signal() -> bool:
    """Track A - extract patterns from real sessions + mistakes.
    Uses last_real_signal_ts state key as lower bound - processes everything since last run.
    Soft-boot default: yesterday, so first run after any deploy rescans last 24h.
    After each successful run, saves current timestamp as the new lower bound.
    """
    try:
        # Read last_real_signal_ts from state (stored in sessions table as [state_update])
        state_rows = sb_get("sessions",
            "select=summary&summary=like.*last_real_signal_ts*&order=created_at.desc&limit=1",
            svc=True)
        if state_rows and state_rows[0].get("summary"):
            raw = state_rows[0]["summary"].split("last_real_signal_ts:")[-1].strip().split()[0]
            since_ts = raw.replace("Z", "").split("+")[0]
            print(f"[RESEARCH/REAL] Using last_real_signal_ts: {since_ts}")
        else:
            # Soft-boot: yesterday so first run after deploy always rescans recent data
            since_ts = (datetime.utcnow() - timedelta(days=1)).isoformat()
            print(f"[RESEARCH/REAL] No state key found, soft-boot to yesterday: {since_ts}")

        sessions = sb_get("sessions",
            f"select=summary,actions,interface&created_at=gte.{since_ts}&order=created_at.desc&limit=20",
            svc=True)
        mistakes = sb_get("mistakes",
            f"select=domain,what_failed,root_cause,how_to_avoid&created_at=gte.{since_ts}&order=id.desc&limit=20",
            svc=True)

        if not sessions and not mistakes:
            print("[RESEARCH/REAL] No new sessions or mistakes since last processed - skipping")
            return False

        # Enrich: KB total count
        try:
            kb_count_r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=8)
            kb_total = int(kb_count_r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            kb_total = 0

        # Enrich: recent changelog entries
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

        raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=800)
        raw = raw.strip()
        if raw.startswith("```"): raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
        result = json.loads(raw.strip())

        patterns = result.get("patterns", [])
        if not patterns:
            print("[RESEARCH/REAL] Groq returned no patterns")
            return False

        ok = sb_post("hot_reflections", {
            "task_summary": f"Real signal extraction (since last processed) - {len(sessions)} sessions, {len(mistakes)} mistakes, kb={kb_total}",
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": result.get("gaps", ""),
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": 0,
            "source": "real",
            "quality_score": None,
        })
        print(f"[RESEARCH/REAL] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        if ok:
            # Save current timestamp as new lower bound for next run
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
    """Read the current custom simulation task from sessions state.
    Returns the task dict if set, or None if using default.
    """
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
                return None  # explicitly cleared
            task = json.loads(raw_val)
            if task and task.get("instruction"):
                return task
    except Exception as e:
        print(f"[SIM] _get_simulation_task error: {e}")
    return None


def _run_simulation_batch() -> bool:
    """Track B - simulation. Uses custom task if set via set_simulation tool,
    falls back to default 1M user population simulation."""
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

        # Enrich: KB total count
        try:
            kb_count_r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=8)
            kb_total = int(kb_count_r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            kb_total = len(kb_sample)

        # Enrich: recently applied evolutions (last 5)
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

        # Build runtime context -- injected into both custom and default prompts
        runtime_context = (
            f"CORE MCP tools ({len(tool_list)}): {', '.join(tool_list[:20])}\n"
            f"KB total entries: {kb_total}\n"
            f"Recently applied evolutions:\n{evos_text}\n"
            f"Known failure modes:\n{failure_modes}\n"
            f"KB domains: {', '.join(kb_domains)}\n"
            f"Sample KB topics: {', '.join(kb_topics_sample)}"
        )

        # Check for custom simulation task
        task = _get_simulation_task()

        if task:
            # Custom simulation -- use owner-defined scenario
            instruction = task.get("instruction", "")
            system = task.get("system_prompt", "")
            user_template = task.get("user_prompt_template", "")
            user = user_template.replace("{{RUNTIME_CONTEXT}}", runtime_context)
            task_summary = f"Custom simulation: {instruction[:150]}"
            print(f"[RESEARCH/SIM] Running custom simulation: {instruction[:80]}")
        else:
            # Default -- hardcoded 1M user population simulation
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

        raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=900)
        raw = raw.strip()
        if raw.startswith("```"): raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
        result = json.loads(raw.strip())

        patterns = result.get("patterns", [])
        if not patterns:
            print("[RESEARCH/SIM] Groq returned no patterns")
            return False

        ok = sb_post("hot_reflections", {
            "task_summary": task_summary,
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": result.get("gaps", ""),
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": 0,
            "source": "simulation",
            "quality_score": None,
        })
        print(f"[RESEARCH/SIM] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/SIM] error: {e}")
        return False


# -- Public source ingestion ---------------------------------------------------
def _ingest_public_sources() -> str:
    """Fetch 2 public GitHub README sources per cycle (rotated by hour).
    Returns combined trimmed content string, or empty string on failure.
    Never raises -- always fails silently.
    """
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
                source_name = url.split("/")[4]  # repo owner as label
                combined.append(f"[{source_name}]\n{text}")
        except Exception:
            pass  # fail silently
    return "\n\n".join(combined)


# -- Background researcher -----------------------------------------------------
def background_researcher():
    global _last_research_run, _last_public_source_run
    print("[RESEARCH] background researcher started - real signal + simulation + public source mode")
    _cycle_count = 0

    while True:
        try:
            now = time.time()
            if now - _last_research_run >= _IMPROVEMENT_INTERVAL:
                print("[RESEARCH] Running signal extraction cycle...")
                _last_research_run = now
                _cycle_count += 1

                # Track B-ext: public source ingestion every 6h
                public_content = ""
                if now - _last_public_source_run >= _PUBLIC_SOURCE_INTERVAL:
                    print("[RESEARCH] Fetching public sources...")
                    public_content = _ingest_public_sources()
                    if public_content:
                        _last_public_source_run = now
                        print(f"[RESEARCH] Public sources fetched: {len(public_content)} chars")
                    else:
                        print("[RESEARCH] Public sources returned empty - skipping")

                real_ok = _extract_real_signal()
                time.sleep(3)
                sim_ok = _run_simulation_batch()

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

                # Telegram cycle summary — only when something happened
                try:
                    if real_ok or sim_ok or auto_applied:
                        parts = []
                        if real_ok:
                            parts.append("new patterns extracted")
                        if sim_ok:
                            parts.append("simulation complete")
                        if auto_applied:
                            parts.append(f"{auto_applied} evolutions auto-applied")
                        if public_content:
                            parts.append("public sources ingested")
                        notify(f"[CORE] Researcher cycle #{_cycle_count}\n" + " | ".join(parts))
                except Exception:
                    pass  # never block loop on notify failure

        except Exception as e:
            print(f"[RESEARCH] loop error: {e}")
        time.sleep(60)


# -- Pattern semantic clustering -----------------------------------------------

def _groq_cluster_patterns(batch_counts: "Counter", batch_domain: dict, batch_sources: dict) -> tuple:
    """Ask Groq to cluster semantically identical/near-identical patterns into canonical keys.
    Returns new (batch_counts, batch_domain, batch_sources) with fragmented keys merged.
    Falls back to original dicts on any failure -- clustering is non-blocking.

    This fixes the core fragmentation bug: same concept expressed with different wording
    creates separate keys that never accumulate past the threshold. Groq reads all raw
    pattern keys and returns a mapping of raw_key -> canonical_key. Keys that are already
    unique/distinct are mapped to themselves.
    """
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
        raw = groq_chat(prompt, model=GROQ_MODEL, max_tokens=2000)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        mapping_by_num = json.loads(raw)  # {"1": "canonical", "2": "canonical", ...}

        # Build raw_key -> canonical_key map
        key_map: dict = {}
        for i, raw_key in enumerate(raw_keys):
            canonical = mapping_by_num.get(str(i + 1), raw_key).strip()
            if not canonical or len(canonical) < 5:
                canonical = raw_key  # fallback to original if Groq returns garbage
            key_map[raw_key] = canonical[:500]  # raised from 200 to 500

        # Rebuild counts/domain/sources with canonical keys
        new_counts: Counter = Counter()
        new_domain: dict = {}
        new_sources: dict = {}
        for raw_key, count in batch_counts.items():
            canonical = key_map.get(raw_key, raw_key)
            new_counts[canonical] += count
            # Domain: keep most specific (first seen wins, same as before)
            new_domain.setdefault(canonical, batch_domain.get(raw_key, "general"))
            # Sources: union of all source sets that merged into this canonical
            existing_src = new_sources.get(canonical, set())
            new_sources[canonical] = existing_src | batch_sources.get(raw_key, {"real"})

        merged_count = len(batch_counts) - len(new_counts)
        print(f"[COLD] Clustering: {len(batch_counts)} raw -> {len(new_counts)} canonical ({merged_count} merged)")
        return new_counts, new_domain, new_sources

    except Exception as e:
        print(f"[COLD] Clustering failed (non-fatal, using raw keys): {e}")
        return batch_counts, batch_domain, batch_sources
