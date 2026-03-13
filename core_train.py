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
NOTE: core.py remains the live entry point until full smoke test passes.
"""
import json
import time
from collections import Counter
from datetime import datetime

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
        summary   = session_data.get("summary", "")
        actions   = session_data.get("actions", []) or []
        interface = session_data.get("interface", "unknown")
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
        new_patterns = []
        quality_score = None
        gaps_identified = None
        try:
            actions_str = ", ".join(str(a) for a in actions[:20])
            prompt = (
                f"Session summary: {summary[:500]}\n"
                f"Actions taken: {actions_str}\n\n"
                f"Extract 2-5 reusable patterns from this session. "
                f"Each pattern should be a short, generalizable rule or observation (under 120 chars). "
                f"Also rate session quality 0.0-1.0 and identify any gaps.\n"
                f"Respond ONLY as JSON: "
                f'{{"patterns": ["..."], "quality": 0.8, "gaps": "..or null"}}'
            )
            raw = groq_chat(prompt, model=GROQ_FAST, max_tokens=300)
            parsed = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
            new_patterns = [p for p in parsed.get("patterns", []) if isinstance(p, str) and len(p) > 5][:5]
            quality_score = float(parsed.get("quality") or 0.7)
            gaps_identified = parsed.get("gaps") or None
            print(f"[HOT] Groq extracted {len(new_patterns)} patterns, quality={quality_score}")
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
def run_cold_processor():
    try:
        hots = sb_get("hot_reflections",
                      "select=id,domain,new_patterns,new_mistakes,quality_score,source&processed_by_cold=eq.0&id=gt.1&order=created_at.asc",
                      svc=True)
        if not hots:
            print("[COLD] No unprocessed hot reflections.")
            return {"ok": True, "processed": 0, "evolutions_queued": 0}

        period_start      = datetime.utcnow().isoformat()
        evolutions_queued = 0
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
                    key = str(p)[:200]
                    batch_counts[key] += 1
                    batch_domain.setdefault(key, h.get("domain", "general"))
                    batch_sources.setdefault(key, set()).add(src)

        all_pf = {r["pattern_key"]: r for r in sb_get(
            "pattern_frequency", "select=id,pattern_key,frequency,auto_applied&limit=2000", svc=True
        ) if r.get("id") != 1 and r.get("pattern_key")}

        for key, batch_count in batch_counts.items():
            existing = all_pf.get(key)
            src_set  = batch_sources.get(key, {"real"})
            src_key  = "both" if len(src_set) > 1 else next(iter(src_set))
            src_mult = _SRC_CONF.get(src_key, 1.0)
            domain   = batch_domain.get(key, "general")

            if existing:
                new_freq = existing["frequency"] + batch_count
                sb_upsert("pattern_frequency",
                          {"id": existing["id"], "pattern_key": key, "frequency": new_freq,
                           "domain": domain, "description": key[:500]},
                          on_conflict="id")
                total_freq = new_freq
            else:
                sb_upsert("pattern_frequency",
                          {"pattern_key": key, "frequency": batch_count,
                           "domain": domain, "description": key[:500], "auto_applied": False},
                          on_conflict="pattern_key")
                total_freq = batch_count

            if total_freq >= PATTERN_EVO_THRESHOLD and not (existing or {}).get("auto_applied"):
                base_conf   = min(0.5 + total_freq * 0.05, 0.95)
                final_conf  = round(base_conf * src_mult, 3)
                ok = sb_post_critical("evolution_queue", {
                    "change_type":    "knowledge",
                    "change_summary": f"Recurring pattern ({total_freq}x, src={src_key}): {key[:200]}",
                    "pattern_key":    key,
                    "confidence":     final_conf,
                    "status":         "pending",
                    "source":         src_key,
                    "impact":         domain,
                    "recommendation": f"Pattern appears {total_freq}x. src_mult={src_mult}. Consider KB entry or tool.",
                })
                if ok:
                    evolutions_queued += 1
                    sb_upsert("pattern_frequency",
                              {"pattern_key": key, "auto_applied": True},
                              on_conflict="pattern_key")

        sb_post_critical("cold_reflections", {
            "period_start": period_start, "period_end": datetime.utcnow().isoformat(),
            "hot_count": len(hots), "patterns_found": len(batch_counts),
            "evolutions_queued": evolutions_queued, "auto_applied": 0,
            "summary_text": f"Processed {len(hots)} hots. {len(batch_counts)} unique patterns. {evolutions_queued} evolutions queued.",
        })
        for h in hots:
            sb_patch("hot_reflections", f"id=eq.{h['id']}", {"processed_by_cold": 1})
        if evolutions_queued > 0:
            notify(f"Cold processor: {evolutions_queued} evolution(s) queued from {len(hots)} sessions.\nReview via Claude Desktop.")
        print(f"[COLD] Done: processed={len(hots)} patterns={len(batch_counts)} evolutions={evolutions_queued}")
        return {"ok": True, "processed": len(hots), "patterns_found": len(batch_counts), "evolutions_queued": evolutions_queued}
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
            applied = sb_post_critical("knowledge_base", {
                "domain": evo.get("impact", "general"),
                "topic": pattern_key or change_summary[:100],
                "content": change_summary,
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
                # In split architecture: t_gh_search_replace must be called via tools import
                # For now, log as pending_desktop for manual application
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
            try:
                meta = json.loads(diff_content) if diff_content else {}
            except Exception:
                meta = {}
            btype    = meta.get("backlog_type", "other")
            executor = meta.get("executor", "auto")
            domain   = meta.get("domain", "general")
            title    = meta.get("title", change_summary[:80])
            desc     = meta.get("description", change_summary)

            if executor == "groq" or (executor == "auto" and btype in ("new_kb", "missing_data")):
                if btype == "new_kb":
                    applied = bool(sb_post("knowledge_base", {
                        "domain": domain, "topic": title, "content": desc,
                        "confidence": "medium", "tags": ["backlog", "auto_applied"],
                        "source": "evolution_queue",
                    }))
                    note = f"[groq] KB entry added: {title}"
                else:
                    task_payload = json.dumps({"task": desc, "domain": domain, "source": "backlog", "title": title})
                    try:
                        _priority = int(float(evo.get("confidence") or 0.5) * 10)
                    except Exception:
                        _priority = 5
                    applied = bool(sb_post("task_queue", {
                        "type": "improvement", "payload": task_payload, "status": "pending",
                        "priority": _priority, "source": "backlog_evolution",
                    }))
                    note = f"[groq] Task queued: {title}"

            elif executor == "claude_desktop" or (executor == "auto" and btype in ("new_tool", "telegram_command")):
                sb_patch("evolution_queue", f"id=eq.{evolution_id}", {"status": "pending_desktop"})
                notify(f"[BACKLOG] Approved - needs Claude Desktop\n"
                       f"Type: {btype} | {title}\n\n"
                       f"Action: In next Claude Desktop session, implement:\n{desc[:300]}\n\n"
                       f"Evolution ID: {evolution_id}")
                applied = True
                note = f"[claude_desktop] Flagged for Desktop session: {title}"

            else:
                plan_prompt = f"Generate a concise implementation plan for: {title}\nDescription: {desc}\nOutput as numbered steps, max 5 steps."
                plan = groq_chat("You are CORE planning engine. Be concise.", plan_prompt,
                                 model=GROQ_FAST, max_tokens=300)
                applied = bool(sb_post("task_queue", {
                    "type": "improvement",
                    "payload": json.dumps({"title": title, "plan": plan, "domain": domain}),
                    "status": "pending", "priority": 5, "source": "backlog_evolution",
                }))
                note = f"[auto] Plan generated + queued: {title}"

            sb_patch("backlog", f"title=eq.{title}",
                     {"status": "done" if btype == "new_kb" else "in_progress"})

        if applied:
            sb_patch("evolution_queue", f"id=eq.{evolution_id}",
                     {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
            notify(f"Evolution #{evolution_id} applied\nType: {change_type}\n{note}")
            try:
                gh_write("BACKLOG.md", _backlog_to_markdown(),
                         f"chore(backlog): sync after evo #{evolution_id} applied [{change_type}]")
            except Exception as _be:
                print(f"[BACKLOG] refresh error: {_be}")
        else:
            if change_type not in ("backlog",):
                notify(f"Evolution #{evolution_id} apply failed\nType: {change_type}")
            else:
                print(f"[EVO] #{evolution_id} backlog apply failed silently")
        return {"ok": applied, "evolution_id": evolution_id, "change_type": change_type, "note": note}
    except Exception as e:
        print(f"[EVO] error: {e}")
        return {"ok": False, "error": str(e)}


def reject_evolution(evolution_id: int, reason: str = ""):
    try:
        rows = sb_get("evolution_queue",
                      f"select=*&id=eq.{evolution_id}&status=eq.pending&limit=1", svc=True)
        if not rows: return {"ok": False, "error": f"Evolution {evolution_id} not found or not pending"}
        sb_patch("evolution_queue", f"id=eq.{evolution_id}", {"status": "rejected"})
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


# -- Cold processor loop -------------------------------------------------------
def cold_processor_loop():
    global _last_cold_run, _last_cold_kb_count
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
                try:
                    gh_write("BACKLOG.md", _backlog_to_markdown(),
                             f"chore(backlog): auto-refresh after cold processor ({trigger})")
                except Exception as be:
                    print(f"[COLD] backlog refresh error: {be}")

            for evo in sb_get("evolution_queue",
                               "select=id,confidence,change_type&status=eq.pending&change_type=eq.knowledge&id=gt.1",
                               svc=True):
                conf = float(evo.get("confidence") or 0)
                if conf >= KNOWLEDGE_AUTO_CONFIDENCE:
                    apply_evolution(evo["id"])
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

        if priority >= 3:
            executor = (
                "claude_desktop" if itype in ("new_tool", "telegram_command") else
                "groq"           if itype in ("new_kb", "missing_data") else
                "auto"
            )
            change_type = "knowledge" if itype == "new_kb" else "backlog"
            auto_apply  = (itype == "new_kb" and effort == "low" and executor == "groq")
            sb_post_critical("evolution_queue", {
                "change_type":    change_type,
                "change_summary": f"[BACKLOG P{priority}][{executor}] {title}: {item.get('description','')[:180]}",
                "diff_content":   json.dumps({
                    "backlog_type": itype, "executor": executor,
                    "domain": domain, "effort": effort,
                    "impact": item.get("impact", "medium"),
                    "title": title, "description": item.get("description", ""),
                }),
                "pattern_key": f"backlog:{itype}:{title[:60]}",
                "confidence":  round(0.5 + priority * 0.08, 2),
                "status":      "applied" if auto_apply else "pending",
                "source":      "background_researcher",
                "impact":      domain,
            })
    return new_items


def _sync_backlog_status():
    """Sync backlog item statuses from evolution_queue."""
    try:
        rows = sb_get("evolution_queue",
                      "select=status,pattern_key&change_type=in.(backlog,knowledge)&order=id.desc&limit=500",
                      svc=True)
        synced = 0
        for row in rows:
            pk = row.get("pattern_key", "")
            if not pk.startswith("backlog:"): continue
            parts = pk.split(":", 2)
            if len(parts) != 3: continue
            title_key = parts[2]
            es = row.get("status", "pending")
            new_status = (
                "done"        if es in ("applied", "done") else
                "in_progress" if es == "pending_desktop" else
                None
            )
            if new_status:
                sb_patch("backlog", f"title=eq.{title_key}", {"status": new_status})
                synced += 1
        return synced
    except Exception as e:
        print(f"[BACKLOG] status sync error: {e}")
        return 0


def _backlog_to_markdown() -> str:
    """Generate BACKLOG.md from Supabase backlog table."""
    _sync_backlog_status()
    try:
        rows = sb_get("backlog", "select=*&order=priority.desc&limit=500", svc=True)
    except Exception as e:
        return f"# CORE Improvement Backlog\n\n_Error reading backlog: {e}_\n"
    if not rows:
        return "# CORE Improvement Backlog\n\n_No items yet._\n"
    total     = len(rows)
    n_done    = sum(1 for b in rows if b.get("status") == "done")
    n_prog    = sum(1 for b in rows if b.get("status") == "in_progress")
    n_pending = total - n_done - n_prog
    lines = [
        "# CORE Improvement Backlog",
        f"\n_Auto-generated. Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_",
        f"_Total: {total} | Pending: {n_pending} | In Progress: {n_prog} | Done: {n_done}_\n",
        "---\n",
    ]
    by_type: dict = {}
    for item in rows:
        by_type.setdefault(item.get("type", "other"), []).append(item)
    type_labels = {
        "new_tool": "New Tools", "logic_improvement": "Logic Improvements",
        "new_kb": "Knowledge Gaps", "telegram_command": "Telegram Commands",
        "performance": "Performance", "missing_data": "Missing Data", "other": "Other",
    }
    status_icon = {"done": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    for t, items in by_type.items():
        n_t_done = sum(1 for i in items if i.get("status") == "done")
        lines.append(f"## {type_labels.get(t, t)} ({n_t_done}/{len(items)} done)\n")
        for item in items:
            p      = item.get("priority", 1)
            status = item.get("status", "pending")
            s_icon = status_icon.get(status, "[ ]")
            lines.append(f"### {s_icon} P{p}: {item.get('title','')}")
            lines.append(f"- **Status:** {status} | **Type:** {t} | **Effort:** {item.get('effort','?')} | **Impact:** {item.get('impact','?')} | **Domain:** {item.get('domain','?')}")
            lines.append(f"- **What:** {item.get('description','')}")
            lines.append(f"- **Discovered:** {item.get('discovered_at','')[:16]}")
            lines.append("")
    lines.append("---\n_CORE runs background_researcher every 60 min._")
    lines.append("_Use `/backlog` in Telegram or `get_backlog` MCP tool to review._")
    return "\n".join(lines)


# -- KB Mining -----------------------------------------------------------------
def run_kb_mining(max_batches: int = 50, force: bool = False) -> dict:
    """Mine KB in batches to populate backlog."""
    try:
        counts = get_system_counts()
        kb_count = counts.get("knowledge_base", 0)
        backlog_count = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])

        if not force and backlog_count >= kb_count / KB_MINE_RATIO_THRESHOLD:
            msg = f"[KB MINE] Skipped - backlog ({backlog_count}) sufficient vs KB ({kb_count})."
            print(msg)
            return {"ok": True, "skipped": True, "reason": msg}

        notify(f"KB Mining started\nScanning {kb_count} KB entries in batches of {KB_MINE_BATCH_SIZE}")
        print(f"[KB MINE] Starting. kb={kb_count} backlog={backlog_count} max_batches={max_batches}")

        total_new = 0
        offset = 0
        batches_done = 0
        system = """You are CORE's KB mining engine. Identify gaps and improvements from KB entries.
Output MUST be a JSON array of 3-5 items:
[{"priority": 1-5, "type": "new_tool|logic_improvement|new_kb|telegram_command|performance|missing_data",
 "title": "short title", "description": "actionable description", "effort": "low|medium|high", "impact": "low|medium|high"}]
Output ONLY valid JSON array, no preamble."""

        while batches_done < max_batches:
            kb_batch = sb_get("knowledge_base",
                              f"select=domain,topic,content&order=id.asc&limit={KB_MINE_BATCH_SIZE}&offset={offset}",
                              svc=True)
            if not kb_batch:
                break

            batch_text = "\n".join([
                f"[{r.get('domain','?')}] {r.get('topic','?')}: {str(r.get('content',''))[:150]}"
                for r in kb_batch
            ])
            domains_in_batch = list({r.get("domain","general") for r in kb_batch})

            user = (f"KB batch ({len(kb_batch)} entries, domains: {', '.join(domains_in_batch)}):\n\n"
                    f"{batch_text}\n\nWhat gaps does CORE need to address?")

            try:
                raw = groq_chat(system, user, model=GROQ_FAST, max_tokens=600)
                raw = raw.strip()
                if raw.startswith("```"): raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
                items = json.loads(raw.strip())
                if isinstance(items, list):
                    for item in items:
                        if not item.get("domain"):
                            item["domain"] = domains_in_batch[0] if domains_in_batch else "general"
                        item["discovered_at"] = datetime.utcnow().isoformat()
                        item["status"] = "pending"
                    new = _backlog_add(items)
                    total_new += len(new)
                    print(f"[KB MINE] Batch {batches_done+1}: offset={offset} new_items={len(new)}")
            except Exception as e:
                print(f"[KB MINE] Batch {batches_done+1} error: {e}")

            offset += KB_MINE_BATCH_SIZE
            batches_done += 1
            if len(kb_batch) < KB_MINE_BATCH_SIZE:
                break
            time.sleep(3)

        final_count = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])

        notify(f"KB Mining complete\nBatches: {batches_done}\nNew items: {total_new}\nTotal backlog: {final_count}")
        print(f"[KB MINE] Done. batches={batches_done} new_items={total_new} total_backlog={final_count}")
        return {"ok": True, "batches_scanned": batches_done, "new_items": total_new,
                "total_backlog": final_count, "kb_count": kb_count}

    except Exception as e:
        print(f"[KB MINE] error: {e}")
        return {"ok": False, "error": str(e)}


# -- Real signal + simulation --------------------------------------------------
def _extract_real_signal() -> bool:
    """Track A - extract patterns from real sessions + mistakes."""
    try:
        sessions = sb_get("sessions",
            "select=summary,actions,interface&order=created_at.desc&limit=20", svc=True)
        mistakes = sb_get("mistakes",
            "select=domain,what_failed,root_cause,how_to_avoid&order=id.desc&limit=20", svc=True)

        if not sessions and not mistakes:
            print("[RESEARCH/REAL] No data yet - skipping")
            return False

        sessions_text = "\n".join([
            f"- [{r.get('interface','?')}] {r.get('summary','')[:200]}"
            for r in sessions
        ]) or "No sessions yet."

        mistakes_text = "\n".join([
            f"- [{r.get('domain','?')}] FAILED: {r.get('what_failed','')[:150]} | ROOT: {r.get('root_cause','')[:100]}"
            for r in mistakes
        ]) or "No mistakes yet."

        system = """You are CORE's pattern extraction engine. Analyze real activity logs.
Output MUST be valid JSON:
{
  "domain": "code|db|bot|mcp|training|kb|general",
  "patterns": ["pattern1", "pattern2"],
  "gaps": "1-2 sentences",
  "summary": "1 sentence"
}
Output ONLY valid JSON, no preamble."""

        user = (f"RECENT SESSIONS ({len(sessions)}):\n{sessions_text}\n\n"
                f"RECENT MISTAKES ({len(mistakes)}):\n{mistakes_text}\n\n"
                f"Extract patterns from this real activity.")

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
            "task_summary": f"Real signal extraction - {len(sessions)} sessions, {len(mistakes)} mistakes",
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": result.get("gaps", ""),
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": False,
            "source": "real",
            "quality_score": None,
        })
        print(f"[RESEARCH/REAL] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/REAL] error: {e}")
        return False


def _run_simulation_batch() -> bool:
    """Track B - grounded simulation of 1M user population."""
    try:
        # TOOLS imported lazily to avoid circular import
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

        system = """You are simulating 1,000,000 users of CORE - a personal AGI orchestration system.
Output MUST be valid JSON:
{
  "domain": "code|db|bot|mcp|training|kb|general",
  "patterns": ["pattern1", "pattern2"],
  "gaps": "1-2 sentences",
  "summary": "1 sentence"
}
Output ONLY valid JSON, no preamble."""

        user = (f"CORE's MCP tools ({len(tool_list)}): {', '.join(tool_list)}\n\n"
                f"Known failure modes:\n{failure_modes}\n\n"
                f"KB domains: {', '.join(kb_domains)}\n"
                f"Sample KB topics: {', '.join(kb_topics_sample)}\n\n"
                f"Simulate 1,000,000 users. What patterns emerge?")

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
            "task_summary": "Simulated 1M user population batch",
            "domain": result.get("domain", "general"),
            "new_patterns": patterns,
            "gaps_identified": result.get("gaps", ""),
            "reflection_text": result.get("summary", ""),
            "processed_by_cold": False,
            "source": "simulation",
            "quality_score": None,
        })
        print(f"[RESEARCH/SIM] ok={ok} patterns={len(patterns)} domain={result.get('domain')}")
        return ok
    except Exception as e:
        print(f"[RESEARCH/SIM] error: {e}")
        return False


# -- Background researcher -----------------------------------------------------
def background_researcher():
    global _last_research_run
    print("[RESEARCH] background researcher started - real signal + simulation mode")

    while True:
        try:
            if time.time() - _last_research_run >= _IMPROVEMENT_INTERVAL:
                print("[RESEARCH] Running signal extraction cycle...")
                _last_research_run = time.time()

                real_ok = _extract_real_signal()
                time.sleep(3)
                sim_ok  = _run_simulation_batch()

                # Auto-apply groq-executable pending evolutions
                try:
                    groq_pending = sb_get(
                        "evolution_queue",
                        "select=id,change_type,change_summary,diff_content,confidence,pattern_key&status=eq.pending&order=id.asc&limit=20",
                        svc=True
                    )
                    auto_applied = 0
                    for evo in groq_pending:
                        try:
                            meta = json.loads(evo.get("diff_content") or "{}")
                        except Exception:
                            meta = {}
                        executor = meta.get("executor", "auto")
                        btype    = meta.get("backlog_type", "")
                        if executor == "groq" or (executor == "auto" and btype in ("new_kb", "missing_data")):
                            r = apply_evolution(evo["id"])
                            if r.get("ok"):
                                auto_applied += 1
                            time.sleep(1)
                    if auto_applied:
                        print(f"[RESEARCH] Auto-applied {auto_applied} groq evolutions")
                except Exception as _ae:
                    print(f"[RESEARCH] auto-apply error: {_ae}")

                # Auto-run KB mining if backlog underpopulated
                try:
                    counts = get_system_counts()
                    kb_count = counts.get("knowledge_base", 0)
                    backlog_count = int(httpx.get(
                        f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
                        headers=_sbh_count_svc(), timeout=10
                    ).headers.get("content-range", "*/0").split("/")[-1])
                    if backlog_count < kb_count / KB_MINE_RATIO_THRESHOLD:
                        print("[RESEARCH] Backlog underpopulated - triggering KB mining")
                        run_kb_mining(max_batches=5)
                except Exception as _me:
                    print(f"[RESEARCH] kb_mine auto-trigger error: {_me}")

        except Exception as e:
            print(f"[RESEARCH] loop error: {e}")
        time.sleep(60)
