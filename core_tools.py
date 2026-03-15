"""core_tools.py — CORE AGI MCP tool implementations
All t_* functions, TOOLS registry, _mcp_tool_schema, handle_jsonrpc.
Part of v6.0 split architecture: core_config, core_github, core_train, core_tools, core_main.

Import chain:
  core_tools imports: core_config, core_github, core_train
  core_main imports: core_tools (TOOLS, handle_jsonrpc)

NOTE: This IS the live implementation. Entry point = core_main.py (Procfile confirmed).
core.py has been deleted — it was legacy monolith."""
import base64
import difflib
import json
import os
import re as _re
import time
from collections import Counter
from datetime import datetime, timedelta

import httpx

from core_config import (
    GITHUB_REPO, GROQ_FAST, GROQ_MODEL, KB_MINE_BATCH_SIZE, KB_MINE_RATIO_THRESHOLD,
    COLD_HOT_THRESHOLD, COLD_KB_GROWTH_THRESHOLD, PATTERN_EVO_THRESHOLD,
    KNOWLEDGE_AUTO_CONFIDENCE, MCP_PROTOCOL_VERSION, SUPABASE_URL, SUPABASE_REF,
    L, groq_chat, sb_get, sb_post, sb_post_critical, sb_patch, sb_upsert, sb_delete,
)
from core_config import _sbh, _sbh_count_svc
from core_github import _ghh, _gh_blob_read, _gh_blob_write, gh_read, gh_write, notify
from core_train import apply_evolution, reject_evolution, bulk_reject_evolutions, run_cold_processor

# Alias — used in t_core_py_rollback and t_deploy_and_wait
notify_owner = notify


# -- Helpers needed locally ---------------------------------------------------
def get_latest_session():
    rows = sb_get("sessions", "select=summary,actions,created_at&order=created_at.desc&limit=1", svc=True)
    return rows[0] if rows else {}

def get_system_counts():
    counts = {}
    for table in ["knowledge_base", "mistakes", "sessions", "task_queue", "hot_reflections", "evolution_queue"]:
        try:
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/{table}?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=10
            )
            counts[table] = int(r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            counts[table] = -1
    return counts

def get_current_step() -> str:
    try:
        content = gh_read("SESSION.md")
        for line in content.splitlines():
            if line.startswith("## Current Step") or "## Step" in line or "Current Step" in line:
                return line.strip()
        return "(step unknown — read SESSION.md)"
    except Exception as e:
        return f"(step read error: {e})"


# -- MCP tool implementations -------------------------------------------------
def t_state(include_operating_context: str = "false"):
    """Read full system state. operating_context.json is NOT loaded by default (slow GitHub read).
    Pass include_operating_context=true to load it explicitly."""
    session = get_latest_session()
    counts  = get_system_counts()
    # Fetch open tasks from both main registries -- pending OR in_progress, ordered by priority
    pending = sb_get(
        "task_queue",
        "select=id,task,status,priority,source&source=in.(core_v6_registry,mcp_session)&status=in.(pending,in_progress)&order=priority.desc&limit=20"
    ) or []
    load_oc = str(include_operating_context).strip().lower() in ("true", "1", "yes")
    if load_oc:
        try:    operating_context = json.loads(gh_read("operating_context.json"))
        except Exception as e: operating_context = {"error": f"failed to load: {e}"}
    else:
        operating_context = None
    try:    session_md = gh_read("SESSION.md")[:5000]
    except Exception as e: session_md = f"SESSION.md unavailable: {e}"
    return {"last_session": session.get("summary", "No sessions yet."),
            "last_actions": session.get("actions", []),
            "last_session_ts": session.get("created_at", ""),
            "counts": counts, "pending_tasks": pending,
            "operating_context": operating_context,
            "operating_context_included": load_oc,
            "session_md": session_md}

def t_health():
    from core_config import GROQ_API_KEY, TELEGRAM_TOKEN
    h = {"ts": datetime.utcnow().isoformat(), "components": {}}
    for name, fn in [
        ("supabase", lambda: sb_get("sessions", "select=id&limit=1")),
        ("groq",     lambda: httpx.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=5).raise_for_status()),
        ("telegram", lambda: httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=5).raise_for_status()),
        ("github",   lambda: gh_read("README.md")),
    ]:
        try: fn(); h["components"][name] = "ok"
        except Exception as e: h["components"][name] = f"error:{e}"
    h["overall"] = "ok" if all(v == "ok" for v in h["components"].values()) else "degraded"
    return h

def t_constitution():
    try:
        with open("constitution.txt") as f: txt = f.read()
    except: txt = gh_read("constitution.txt")
    return {"constitution": txt, "immutable": True}

def t_search_kb(query="", domain="", limit=10):
    """Search knowledge_base. Multi-word queries search content, topic, and instruction fields."""
    lim = int(limit) if limit else 10
    qs = f"select=domain,topic,instruction,content,confidence&limit={lim}"
    if domain and domain not in ("all", ""):
        qs += f"&domain=eq.{domain}"
    if query:
        q = query.strip().replace("'", "").replace('"', "")
        qs += f"&or=(content.ilike.*{q}*,topic.ilike.*{q}*,instruction.ilike.*{q}*)"
    return sb_get("knowledge_base", qs)

def t_get_mistakes(domain="", limit=10):
    try: lim = int(limit) if limit else 10
    except: lim = 10
    qs = f"select=domain,context,what_failed,correct_approach,severity,root_cause,how_to_avoid&order=created_at.desc&limit={lim}"
    if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
    return sb_get("mistakes", qs, svc=True)

def t_update_state(key, value, reason):
    ok = sb_post("sessions", {"summary": f"[state_update] {key}: {str(value)[:200]}",
                              "actions": [f"{key}={str(value)[:100]} - {reason}"], "interface": "mcp"})
    return {"ok": ok, "key": key}

def t_add_knowledge(domain, topic, instruction="", content="", tags="", confidence="medium"):
    """Add knowledge entry. instruction = behavioral directive for CORE (primary). content = supporting detail. At least one required."""
    if not instruction and not content:
        return {"ok": False, "error": "At least one of instruction or content is required"}
    tags_list = [t.strip() for t in tags.split(",")] if tags else []
    ok = sb_post("knowledge_base", {"domain": domain, "topic": topic, "instruction": instruction or None,
                                    "content": content or "", "confidence": confidence,
                                    "tags": tags_list, "source": "mcp_session"})
    return {"ok": ok, "topic": topic}

def t_set_simulation(instruction: str) -> dict:
    """Set a custom simulation task for the background researcher.
    CORE crafts the Groq prompts from your instruction and stores them.
    The background researcher loops on this every 60 min until you change it.
    Call with empty instruction to reset to default 1M user simulation.
    """
    try:
        instruction = (instruction or "").strip()
        if not instruction:
            # Clear custom simulation -- reset to default
            ok = sb_post("sessions", {
                "summary": "[state_update] simulation_task: null",
                "actions": ["simulation_task cleared -- reset to default 1M user simulation"],
                "interface": "mcp"
            })
            notify("Simulation reset to default (1M user population simulation)")
            return {"ok": ok, "cleared": True, "message": "Reset to default simulation"}

        # Craft system prompt
        system_prompt = (
            "You are CORE's simulation engine. Your job is to simulate the scenario described below "
            "and extract actionable patterns that CORE should learn from. "
            "Output MUST be valid JSON: "
            '{"domain": "code|db|bot|mcp|training|kb|general", '
            '"patterns": ["pattern1", "pattern2", "pattern3"], '
            '"gaps": "1-2 sentences on gaps found", '
            '"summary": "1 sentence summary"} '
            "Output ONLY valid JSON, no preamble."
        )

        # Craft user prompt -- dynamic context injected at runtime by _run_simulation_batch
        user_prompt_template = (
            f"Simulation scenario: {instruction}\n\n"
            "CORE context (injected at runtime):\n"
            "{{RUNTIME_CONTEXT}}\n\n"
            f"Run this simulation. Extract patterns CORE should learn from. "
            f"Focus specifically on: {instruction}"
        )

        task = {
            "instruction": instruction,
            "system_prompt": system_prompt,
            "user_prompt_template": user_prompt_template,
            "set_at": __import__('datetime').datetime.utcnow().isoformat(),
        }

        ok = sb_post("sessions", {
            "summary": f"[state_update] simulation_task: {json.dumps(task)}",
            "actions": [f"simulation_task set: {instruction[:200]}"],
            "interface": "mcp"
        })
        if ok:
            notify(f"Simulation task set\nScenario: {instruction[:200]}\nBackground researcher will use this every 60 min.")
        return {"ok": ok, "instruction": instruction, "message": "Simulation task stored. Background researcher will pick it up on next cycle."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_log_mistake(context, what_failed, correct_approach, domain="general", root_cause="", how_to_avoid="", severity="medium"):
    ok = sb_post("mistakes", {"domain": domain, "context": context, "what_failed": what_failed,
                              "correct_approach": correct_approach, "root_cause": root_cause or what_failed,
                              "how_to_avoid": how_to_avoid or correct_approach, "severity": severity, "tags": []})
    return {"ok": ok}

def t_read_file(path, repo="", start_line="", end_line=""):
    """Read a GitHub file. Optional start_line/end_line for range. Cap 8000 chars with truncated flag. Use gh_read_lines for large files."""
    try:
        raw = gh_read(path, repo or GITHUB_REPO)
        lines = raw.splitlines(keepends=True)
        total = len(lines)
        if start_line or end_line:
            s = max(0, int(start_line) - 1) if start_line else 0
            e = int(end_line) if end_line else total
            lines = lines[s:e]
            raw = "".join(lines)
        truncated = len(raw) > 8000
        return {"ok": True, "content": raw[:8000], "total_line_count": total, "truncated": truncated}
    except Exception as e: return {"ok": False, "error": str(e)}

def t_write_file(path, content, message, repo=""):
    """Write file to GitHub repo - FULL OVERWRITE. Use for NEW files only.
    GUARD: blocked for core_main.py and core_tools.py - use patch_file or gh_search_replace for surgical edits."""
    blocked = {"core_main.py", "core_tools.py"}
    clean_path = path.strip().lstrip("/")
    if (repo or GITHUB_REPO) == GITHUB_REPO and clean_path in blocked:
        return {
            "ok": False,
            "error": f"BLOCKED: write_file cannot overwrite {clean_path} (full overwrite = corruption risk). "
                     "Use patch_file or gh_search_replace for surgical edits."
        }
    if clean_path.endswith(".py"):
        import tempfile, py_compile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(content); tmp = f.name
        try:
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            os.unlink(tmp)
            return {"ok": False, "error": f"Syntax error: {e}"}
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    ok = gh_write(path, content, message, repo or GITHUB_REPO)
    if ok: notify(f"MCP write: `{clean_path}`")
    return {"ok": ok, "path": path}

def t_notify(message, level="info"):
    icons = {"info": "i", "warn": "!", "alert": "ALERT", "ok": "OK"}
    return {"ok": notify(f"{icons.get(level, '>')} CORE\n{message}")}

def t_sb_query(table, filters="", limit=20, order="", select="*"):
    """Raw Supabase read. Use dedicated tools first (search_kb, get_mistakes etc) -- this is the escape hatch.
    filters: PostgREST filter string e.g. 'status=eq.pending'.
    order: sort column e.g. 'created_at.desc'.
    select: columns to return e.g. 'id,status,task' (default *)."""
    try: lim = int(limit) if limit else 20
    except: lim = 20
    sel = select.strip() if select and select.strip() else "*"
    qs = f"select={sel}"
    if filters and filters.strip():
        qs += f"&{filters.strip()}"
    if order and order.strip():
        qs += f"&order={order.strip()}"
    qs += f"&limit={lim}"
    return sb_get(table, qs, svc=True)

def t_sb_insert(table, data):
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception as e: return {"ok": False, "error": f"data must be valid JSON: {e}"}
    return {"ok": sb_post(table, data), "table": table}

def t_sb_bulk_insert(table: str, rows: str) -> dict:
    """Insert multiple rows into Supabase in a single HTTP call."""
    try:
        if isinstance(rows, str):
            rows = json.loads(rows)
        if not isinstance(rows, list):
            return {"ok": False, "error": "rows must be a JSON array"}
        if len(rows) == 0:
            return {"ok": False, "error": "rows array is empty"}
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**_sbh(True), "Prefer": "return=minimal"},
            json=rows,
            timeout=30
        )
        ok = r.is_success
        if not ok:
            print(f"[SB BULK] {table} failed: {r.status_code} {r.text[:200]}")
        return {"ok": ok, "table": table, "rows_attempted": len(rows),
                "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_get_training_pipeline() -> dict:
    """Full training pipeline status: hot reflections, cold processor, patterns, quality trend, health flags.
    Used by session_start training_pipeline block and /tstatus Telegram command."""
    try:
        now = datetime.utcnow()
        health_flags = []

        # --- Hot reflections ---
        all_hots = sb_get("hot_reflections",
            "select=id,created_at,source,domain,quality_score,processed_by_cold"
            "&order=created_at.desc&limit=5", svc=True) or []
        unprocessed = [h for h in all_hots if not h.get("processed_by_cold")]
        last_real = next((h for h in all_hots if h.get("source") == "real"), None)
        last_sim  = next((h for h in all_hots if h.get("source") == "simulation"), None)

        # Total counts
        total_hots_rows = sb_get("hot_reflections", "select=id&order=id.desc&limit=1", svc=True) or []
        total_hots = total_hots_rows[0]["id"] if total_hots_rows else 0
        total_sim  = len(sb_get("hot_reflections", "select=id&source=eq.simulation", svc=True) or [])

        # Simulation health flag
        if total_sim == 0:
            health_flags.append("simulation_dead")

        # --- Cold processor ---
        cold_rows = sb_get("cold_reflections",
            "select=id,created_at,hot_count,patterns_found,evolutions_queued,auto_applied,summary_text"
            "&order=created_at.desc&limit=5", svc=True) or []
        last_cold = cold_rows[0] if cold_rows else None
        total_cold_runs = len(cold_rows)  # approximate from recent 5

        cold_mins_ago = None
        if last_cold and last_cold.get("created_at"):
            try:
                ts = datetime.fromisoformat(last_cold["created_at"].replace("Z","").split("+")[0])
                cold_mins_ago = int((now - ts).total_seconds() / 60)
            except Exception:
                pass

        # Cold stale flag: hasn't run in 3+ hours and there are unprocessed hots
        if cold_mins_ago is not None and cold_mins_ago > 180 and len(unprocessed) > 0:
            health_flags.append(f"cold_stale_{cold_mins_ago}min")

        # Unprocessed backlog flag
        unprocessed_count = len(sb_get("hot_reflections",
            "select=id&processed_by_cold=eq.0", svc=True) or [])
        if unprocessed_count >= COLD_HOT_THRESHOLD:
            health_flags.append(f"unprocessed_backlog_{unprocessed_count}_threshold_{COLD_HOT_THRESHOLD}")

        # Zero patterns in last 5 cold runs
        if cold_rows and all(r.get("patterns_found", 0) == 0 for r in cold_rows[:5]):
            health_flags.append("zero_patterns_last_5_runs")

        # --- Patterns ---
        active_patterns = sb_get("pattern_frequency",
            "select=pattern_key,domain,frequency,auto_applied,last_seen"
            "&stale=eq.false&frequency=gte.2&order=frequency.desc&limit=1", svc=True) or []
        all_active = sb_get("pattern_frequency", "select=id&stale=eq.false", svc=True) or []
        stale_count = len(sb_get("pattern_frequency", "select=id&stale=eq.true", svc=True) or [])
        top_pattern = active_patterns[0] if active_patterns else None

        # --- Evolution queue ---
        pending_evo = sb_get("evolution_queue",
            "select=id&status=eq.pending", svc=True) or []
        applied_evo = sb_get("evolution_queue",
            "select=id&status=eq.applied&order=id.desc&limit=1", svc=True) or []

        # --- Quality trend (last 7d) ---
        cutoff = (now - timedelta(days=7)).isoformat()
        quality_rows = sb_get("hot_reflections",
            f"select=quality_score,created_at&source=eq.real&quality_score=not.is.null"
            f"&quality_score=lte.1.0&created_at=gte.{cutoff}&order=created_at.asc", svc=True) or []
        quality_avg = round(sum(r["quality_score"] for r in quality_rows) / len(quality_rows), 3) if quality_rows else None
        # Trend: compare first half vs second half
        quality_trend = "no_data"
        if len(quality_rows) >= 4:
            mid = len(quality_rows) // 2
            first = sum(r["quality_score"] for r in quality_rows[:mid]) / mid
            second = sum(r["quality_score"] for r in quality_rows[mid:]) / len(quality_rows[mid:])
            quality_trend = "improving" if second - first > 0.03 else ("declining" if first - second > 0.03 else "stable")
            if quality_trend == "declining":
                health_flags.append("quality_declining")

        return {
            "hot": {
                "total": total_hots,
                "unprocessed": unprocessed_count,
                "total_simulation": total_sim,
                "simulation_ok": total_sim > 0,
                "last_real": {
                    "id": last_real["id"], "ts": last_real["created_at"][:16],
                    "domain": last_real["domain"], "quality": last_real["quality_score"]
                } if last_real else None,
                "last_simulation": {
                    "id": last_sim["id"], "ts": last_sim["created_at"][:16],
                    "domain": last_sim["domain"], "quality": last_sim["quality_score"]
                } if last_sim else None,
            },
            "cold": {
                "last_run_ts": last_cold["created_at"][:16] if last_cold else None,
                "last_run_mins_ago": cold_mins_ago,
                "threshold": COLD_HOT_THRESHOLD,
                "last_hot_count": last_cold["hot_count"] if last_cold else 0,
                "last_patterns_found": last_cold["patterns_found"] if last_cold else 0,
                "last_evolutions_queued": last_cold["evolutions_queued"] if last_cold else 0,
                "last_auto_applied": last_cold["auto_applied"] if last_cold else 0,
                "recent_5_summaries": [r.get("summary_text","")[:80] for r in cold_rows[:5]],
            },
            "patterns": {
                "active_count": len(all_active),
                "stale_count": stale_count,
                "top": {
                    "key": top_pattern["pattern_key"][:80],
                    "domain": top_pattern["domain"],
                    "freq": top_pattern["frequency"],
                } if top_pattern else None,
            },
            "evolutions": {
                "pending": len(pending_evo),
                "applied": int(sb_get("evolution_queue", "select=id&status=eq.applied&order=id.desc&limit=1",
                    svc=True)[0]["id"]) if applied_evo else 0,
            },
            "quality": {
                "7d_avg": quality_avg,
                "trend": quality_trend,
                "sample_count": len(quality_rows),
            },
            "health_flags": health_flags,
            "pipeline_ok": len(health_flags) == 0,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "health_flags": ["pipeline_error"]}


def t_training_status():
    """Legacy wrapper -- use t_get_training_pipeline() for full status."""
    try:
        tp = t_get_training_pipeline()
        unprocessed_count = tp.get("hot", {}).get("unprocessed", 0)
        pending_evo_rows = sb_get("evolution_queue",
            "select=id,change_type,change_summary,confidence&status=eq.pending&id=gt.1", svc=True) or []
        return {
            "status": "Training pipeline ACTIVE",
            "unprocessed_hot": unprocessed_count,
            "pending_evolutions": len(pending_evo_rows),
            "backlog_pending": -1,
            "evolutions": pending_evo_rows[:5],
            "cold_threshold": COLD_HOT_THRESHOLD,
            "kb_growth_threshold": COLD_KB_GROWTH_THRESHOLD,
            "kb_mine_ratio_threshold": KB_MINE_RATIO_THRESHOLD,
            "pattern_threshold": PATTERN_EVO_THRESHOLD,
            "auto_apply_conf": KNOWLEDGE_AUTO_CONFIDENCE,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}

def t_trigger_cold_processor(): return run_cold_processor()

def t_list_evolutions(status="pending"):
    rows = sb_get("evolution_queue",
                  f"select=id,status,change_type,change_summary,confidence,pattern_key,created_at&status=eq.{status}&id=gt.1&order=created_at.desc&limit=20",
                  svc=True)
    return {"evolutions": rows, "count": len(rows)}


def t_bulk_reject_evolutions(change_type: str = "", ids: str = "", reason: str = "", include_synthesized: str = "false") -> dict:
    """Bulk reject pending evolutions silently — one Telegram summary at end.
    change_type: 'backlog' | 'knowledge' | '' (all pending).
    ids: comma-separated evolution IDs (overrides change_type).
    include_synthesized: 'true' to also reject status=synthesized items.
    reason: optional rejection reason."""
    id_list = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()] if ids else []
    inc_syn = str(include_synthesized).lower() in ("true", "1", "yes")
    return bulk_reject_evolutions(change_type=change_type, ids=id_list or None, reason=reason, include_synthesized=inc_syn)


def t_check_evolutions(limit: int = 20) -> dict:
    """Groq-powered evolution brief."""
    try:
        lim = int(limit) if limit else 20
        evolutions = sb_get("evolution_queue",
            f"select=id,change_type,change_summary,confidence,source,recommendation,pattern_key,created_at"
            f"&status=eq.pending&id=gt.1&order=confidence.desc&limit={lim}",
            svc=True)
        mistakes = sb_get("mistakes",
            "select=domain,context,what_failed,correct_approach,root_cause,how_to_avoid,severity"
            "&order=id.desc&limit=10",
            svc=True)
        patterns = sb_get("pattern_frequency",
            "select=pattern_key,frequency,domain,description&stale=eq.false&order=frequency.desc&limit=10",
            svc=True)
        templates = sb_get("script_templates",
            "select=name,description,trigger_pattern&order=use_count.desc&limit=5",
            svc=True)
        tool_names = list(TOOLS.keys())

        evo_text = "\n".join([
            f"[#{e['id']} | {e['change_type']} | conf={e.get('confidence','?')} | src={e.get('source','?')}]\n"
            f"  Summary: {e.get('change_summary','')[:120]}\n"
            f"  Recommendation: {e.get('recommendation','') or 'none'}"
            for e in evolutions
        ]) or "No pending evolutions."

        mistake_text = "\n".join([
            f"[{m.get('domain','?')} | sev={m.get('severity','?')}] FAILED: {m.get('what_failed','')[:100]}\n"
            f"  ROOT: {m.get('root_cause','')[:80]} | FIX: {m.get('correct_approach','')[:100]}"
            for m in mistakes
        ]) or "No mistakes recorded."

        pattern_text = "\n".join([
            f"  [{p.get('domain','?')}] {p.get('pattern_key','')[:80]} (seen {p.get('frequency',0)}x)"
            for p in patterns
        ]) or "No patterns yet."

        template_text = "\n".join([
            f"  TEMPLATE: {t.get('name','')} - {t.get('description','')[:80]}"
            for t in templates
        ]) or "  No templates yet."

        system = """You are CORE's evolution engine. Generate a precise, actionable brief for Claude to act on NOW.

Output MUST be a JSON object:
{
  "session_title": "short title",
  "priority_actions": [
    {
      "rank": 1,
      "action_type": "code_patch | new_tool | new_template | kb_entry | reject",
      "title": "short title",
      "why": "1 sentence",
      "evolution_ids": [list of IDs],
      "ready_to_execute": true/false,
      "instruction": "exact instruction for Claude",
      "code_snippet": "Python code or null"
    }
  ],
  "new_tools_proposed": [{"name": "t_...", "purpose": "...", "trigger": "...", "code": "..."}],
  "templates_proposed": [{"name": "...", "description": "...", "trigger_pattern": "...", "code": "..."}],
  "reject_ids": [list of IDs to reject],
  "summary": "2-3 sentence summary"
}
Output ONLY valid JSON, no preamble."""

        user = (
            f"CORE MCP tools available: {', '.join(tool_names)}\n\n"
            f"PENDING EVOLUTIONS ({len(evolutions)}):\n{evo_text}\n\n"
            f"RECENT MISTAKES ({len(mistakes)}):\n{mistake_text}\n\n"
            f"TOP PATTERNS:\n{pattern_text}\n\n"
            f"EXISTING TEMPLATES:\n{template_text}\n\n"
            f"Generate the evolution brief for this session."
        )

        raw = groq_chat(system, user, model=GROQ_MODEL, max_tokens=2000)
        raw = raw.strip()
        if raw.startswith("```"): raw = "\n".join(raw.split("\n")[1:])
        if raw.endswith("```"): raw = "\n".join(raw.split("\n")[:-1])
        brief = json.loads(raw.strip())

        for tpl in brief.get("templates_proposed", []):
            try:
                sb_post("script_templates", {
                    "name": tpl.get("name", ""),
                    "description": tpl.get("description", ""),
                    "trigger_pattern": tpl.get("trigger_pattern", ""),
                    "code": tpl.get("code", ""),
                    "use_count": 0,
                    "created_at": datetime.utcnow().isoformat(),
                })
            except Exception: pass

        return {
            "ok": True,
            "brief": brief,
            "evolution_count": len(evolutions),
            "mistake_count": len(mistakes),
            "pattern_count": len(patterns),
        }

    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Groq returned invalid JSON: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_approve_evolution(evolution_id):
    try: eid = int(evolution_id)
    except: return {"ok": False, "error": "evolution_id must be a number"}
    return apply_evolution(eid)

def t_reject_evolution(evolution_id, reason=""):
    try: eid = int(evolution_id)
    except: return {"ok": False, "error": "evolution_id must be a number"}
    return reject_evolution(eid, reason)

def t_gh_search_replace(path, old_str, new_str, message, repo="", dry_run="false"):
    """Surgical find-replace using Contents API (gh_read/gh_write) — 2 HTTP calls, proven stable."""
    try:
        repo = repo or GITHUB_REPO
        file_content = gh_read(path, repo)
        if old_str not in file_content:
            return {"ok": False, "error": f"old_str not found in {path}"}
        count = file_content.count(old_str)
        if count > 1:
            return {"ok": False, "error": f"old_str found {count}x - be more specific"}
        new_content = file_content.replace(old_str, new_str, 1)
        if str(dry_run).lower() == "true":
            diff = list(difflib.unified_diff(
                file_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"{path} (before)", tofile=f"{path} (after)", n=3
            ))
            return {"ok": True, "dry_run": True, "path": path,
                    "would_replace": old_str[:80], "diff": "".join(diff)[:3000]}
        ok = gh_write(path, new_content, message, repo)
        if not ok:
            return {"ok": False, "error": "gh_write returned False"}
        return {"ok": True, "dry_run": False, "path": path, "replaced": old_str[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_gh_read_lines(path, start_line=1, end_line=50, repo=""):
    try:
        file_content = _gh_blob_read(path, repo or GITHUB_REPO)
        lines = file_content.splitlines()
        total = len(lines)
        s = max(1, int(start_line)) - 1
        e = min(total, int(end_line))
        selected = lines[s:e]
        numbered = "\n".join(f"{s+i+1:4d}  {line}" for i, line in enumerate(selected))
        return {"ok": True, "path": path, "total_lines": total,
                "showing": f"{s+1}-{s+len(selected)}", "content": numbered}
    except Exception as ex:
        return {"ok": False, "error": str(ex), "path": path}


# -- Agentic speed tools ------------------------------------------------------

def t_core_py_fn(fn_name: str, file: str = "core_tools.py") -> dict:
    """Read a single function from a CORE source file by name. Defaults to core_tools.py."""
    try:
        target = file if file else "core_tools.py"
        content = _gh_blob_read(target)
        lines = content.splitlines()
        start = None
        indent = None
        for i, line in enumerate(lines):
            if line.strip().startswith(f"def {fn_name}(") or line.strip() == f"def {fn_name}()":
                start = i
                indent = len(line) - len(line.lstrip())
                break
        if start is None:
            return {"ok": False, "error": f"Function '{fn_name}' not found in {target}"}
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if line.strip() == "":
                end += 1
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= indent and line.strip().startswith("def "):
                break
            end += 1
        source = "\n".join(lines[start:end])
        return {"ok": True, "fn_name": fn_name, "start_line": start + 1,
                "end_line": end, "line_count": end - start, "source": source}
    except Exception as e:
        return {"ok": False, "error": str(e)}



# -- system_map scan ----------------------------------------------------------

def t_system_map_scan(trigger: str = "manual") -> dict:
    """Scan system_map table - snapshot at session_start, drift-fix at session_end.
    session_start: read-only, returns full wiring for Claude context.
    session_end: read-write, updates volatile key_facts (tool_count etc) if changed.
    manual: same as session_start (read-only snapshot).
    """
    try:
        rows = sb_get(
            "system_map",
            "select=id,layer,component,item_type,name,role,responsibility,key_facts,is_volatile,status"
            "&order=layer,component,name",
            svc=True
        )
        if not isinstance(rows, list):
            return {"ok": False, "error": "system_map query failed", "rows": []}

        updates = []
        inserted_tools = []
        tombstoned_tools = []
        if trigger == "session_end":
            live_tool_count = len(TOOLS)
            # --- Update volatile key_facts (tool_count on core_tools.py row) ---
            for row in rows:
                if not row.get("is_volatile"):
                    continue
                kf = row.get("key_facts") or {}
                new_kf = dict(kf)
                changed = False
                if row["name"] == "core_tools.py" and row["component"] == "railway":
                    if kf.get("tool_count") != live_tool_count:
                        new_kf["tool_count"] = live_tool_count
                        changed = True
                if changed:
                    sb_patch("system_map", row["id"], {
                        "key_facts": new_kf,
                        "last_updated": datetime.utcnow().isoformat(),
                        "updated_by": "session_end"
                    })
                    updates.append({"name": row["name"], "updated_fields": list(new_kf.keys())})

            # --- Auto-reconcile tool entries: insert missing, tombstone removed ---
            # Uses TOOLS dict directly at runtime -- works regardless of how many
            # source files exist or how CORE evolves. No file scanning. No patterns.
            live_tool_names = set(TOOLS.keys())
            registered = {
                row["name"]: row
                for row in rows
                if row.get("component") == "railway"
                and row.get("item_type") == "tool"
                and row.get("status") != "tombstone"
            }
            registered_names = set(registered.keys())

            # Insert tools that exist in TOOLS but not in system_map
            missing = live_tool_names - registered_names
            for tool_name in sorted(missing):
                try:
                    desc = TOOLS[tool_name].get("desc", "")
                    role = desc if desc else f"MCP tool: {tool_name}"
                    sb_post_critical("system_map", {
                        "layer": "executor",
                        "component": "railway",
                        "item_type": "tool",
                        "name": tool_name,
                        "role": role,
                        "responsibility": "auto-registered by session_end reconciliation",
                        "status": "active",
                        "updated_by": "session_end_auto",
                        "last_updated": datetime.utcnow().isoformat(),
                    })
                    inserted_tools.append(tool_name)
                except Exception as _ie:
                    print(f"[SMAP] insert {tool_name} failed: {_ie}")

            # Tombstone tools in system_map that are no longer in TOOLS
            removed = registered_names - live_tool_names
            for tool_name in sorted(removed):
                try:
                    row_id = registered[tool_name]["id"]
                    sb_patch("system_map", f"id=eq.{row_id}", {
                        "status": "tombstone",
                        "notes": "auto-tombstoned by session_end: not in TOOLS dict",
                        "last_updated": datetime.utcnow().isoformat(),
                        "updated_by": "session_end_auto",
                    })
                    tombstoned_tools.append(tool_name)
                except Exception as _te:
                    print(f"[SMAP] tombstone {tool_name} failed: {_te}")

            # --- 16.A: Auto-reconcile brain layer (Supabase tables) ---
            _reconcile_brain_tables(rows, inserted_tools, tombstoned_tools)

            # --- 16.B: Auto-reconcile executor layer (.py source files) ---
            _reconcile_executor_files(rows, inserted_tools, tombstoned_tools)

            # --- 16.C: Auto-reconcile skeleton layer (.md/.json/.txt docs) ---
            _reconcile_skeleton_docs(rows, inserted_tools, tombstoned_tools)

        wiring = {}
        for row in rows:
            if row.get("status") == "tombstone":
                continue  # exclude tombstoned components from live wiring snapshot
            layer = row["layer"]
            if layer not in wiring:
                wiring[layer] = []
            wiring[layer].append({
                "component": row["component"],
                "type": row["item_type"],
                "name": row["name"],
                "role": row["role"],
                "responsibility": row["responsibility"],
            })

        active_rows = [r for r in rows if r.get("status") != "tombstone"]
        return {
            "ok": True,
            "trigger": trigger,
            "total_components": len(active_rows),
            "updates_applied": len(updates),
            "updates": updates,
            "inserted_tools": inserted_tools,
            "tombstoned_tools": tombstoned_tools,
            "wiring": wiring,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get_stale_pattern_count() -> int:
    """Count patterns with stale=true. Used in session_start to surface dead patterns."""
    try:
        rows = sb_get("pattern_frequency", "select=id&stale=eq.true", svc=True) or []
        return len(rows)
    except Exception:
        return 0


def t_session_start() -> dict:
    """One-call session bootstrap - includes system_map snapshot.
    Returns in_progress_tasks separately from pending_tasks so Claude immediately
    knows if a task was left partially done last session.
    recent_mistakes: last 10 across all domains, ordered by recency.
    Use get_mistakes(domain=X) for domain-specific lookup before any write."""
    try:
        state = t_state()
        health = t_health()
        mistakes = t_get_mistakes(domain="", limit=10)
        try:
            evolutions = sb_get("evolution_queue",
                "select=id,change_summary,change_type,confidence&status=eq.pending&order=confidence.desc&limit=5")
            if not isinstance(evolutions, list):
                evolutions = []
        except Exception:
            evolutions = []
        training = t_get_training_pipeline()
        try:
            smap = t_system_map_scan(trigger="session_start")
        except Exception as e:
            smap = {"ok": False, "error": f"system_map scan failed: {e}"}
        # --- 16.E: Drift summary per layer ---
        drift = {"tools": 0, "brain_tables": 0, "executor_files": 0, "skeleton_docs": 0}
        try:
            wiring = smap.get("wiring", {})
            # Tools: live TOOLS dict vs registered active tool entries
            registered_tools = sum(
                1 for r in wiring.get("executor", [])
                if r.get("type") == "tool" and r.get("name") in TOOLS
            )
            drift["tools"] = max(0, len(TOOLS) - registered_tools)
            # Brain tables: count active brain table entries
            registered_brain = sum(
                1 for r in wiring.get("brain", [])
                if r.get("type") == "table"
            )
            # We don't know live table count here (needs mgmt API) -- show registered count
            drift["brain_tables"] = registered_brain  # informational, not a gap count
            # Executor files: count registered .py files
            drift["executor_files"] = sum(
                1 for r in wiring.get("executor", [])
                if r.get("type") == "file"
            )
            # Skeleton docs: count registered skeleton file entries
            drift["skeleton_docs"] = sum(
                1 for r in wiring.get("skeleton", [])
                if r.get("type") == "file"
            )
        except Exception:
            pass
        return {
            "ok": True,
            "health": health.get("overall", "unknown"),
            "components": health.get("components", {}),
            "counts": state.get("counts", {}),
            "last_session": state.get("last_session", ""),
            "last_session_ts": state.get("last_session_ts", ""),
            "in_progress_tasks": [t for t in state.get("pending_tasks", []) if t.get("status") == "in_progress"],
            "pending_tasks": [t for t in state.get("pending_tasks", []) if t.get("status") == "pending"],
            "resume_task": next((t for t in state.get("pending_tasks", []) if t.get("status") == "in_progress"), None),
            "session_md": state.get("session_md", ""),  # full SESSION.md content for claude.ai bootstrap
            "recent_mistakes": mistakes if isinstance(mistakes, list) else [],
            "pending_evolutions": evolutions[:5] if isinstance(evolutions, list) else [],
            "training_pipeline": training,
            "live_tool_count": len(TOOLS),
            "system_map_drift": drift,
            "system_map": smap,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_core_py_validate() -> dict:
    """Pre-deploy syntax checker for core_tools.py and core_main.py."""
    try:
        results = {}
        for target in ["core_tools.py", "core_main.py"]:
            content = _gh_blob_read(target)
            lines = content.splitlines()
            errors = []
            warnings = []
            size_kb = round(len(content.encode()) / 1024, 1)
            line_count = len(lines)
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("def def ") or stripped.startswith("import import "):
                    errors.append(f"L{i}: double keyword — {stripped[:60]}")
            if target == "core_tools.py":
                tool_fn_refs = _re.findall(r'"fn":\s*(t_\w+)', content)
                defined_fns  = set(_re.findall(r'^def (t_\w+)\(', content, _re.MULTILINE))
                for ref in tool_fn_refs:
                    if ref not in defined_fns:
                        errors.append(f"TOOLS refs '{ref}' but function not defined")
                if "TOOLS = {" not in content:
                    errors.append("TOOLS dict not found — critical corruption")
            for i, line in enumerate(lines, 1):
                if "backboard.railway" in line:
                    errors.append(f"L{i}: stale backboard.railway reference")
                if "core.py" in line and not line.strip().startswith("#"):
                    warnings.append(f"L{i}: stale core.py reference — file deleted")
            if size_kb > 150:
                warnings.append(f"{target} is {size_kb}KB — consider splitting (>150KB)")
            triple_count = content.count('"""')
            if triple_count % 2 != 0:
                warnings.append(f"Odd number of triple-quotes ({triple_count}) — possible unclosed docstring")
            results[target] = {"ok": len(errors) == 0, "errors": errors,
                               "warnings": warnings, "line_count": line_count, "size_kb": size_kb}
        overall_ok = all(r["ok"] for r in results.values())
        return {"ok": overall_ok, "files": results}
    except Exception as e:
        return {"ok": False, "error": str(e), "errors": [str(e)], "warnings": []}


def t_search_in_file(path: str, pattern: str, repo: str = "",
                     regex: str = "false", case_sensitive: str = "false") -> dict:
    """Search for a pattern in a GitHub file."""
    try:
        content = _gh_blob_read(path, repo or GITHUB_REPO)
        lines = content.splitlines()
        matches = []
        use_regex = str(regex).lower() == "true"
        use_case  = str(case_sensitive).lower() == "true"
        flags = 0 if use_case else _re.IGNORECASE
        for i, line in enumerate(lines, 1):
            if use_regex:
                if _re.search(pattern, line, flags):
                    matches.append({"line": i, "content": line})
            else:
                hay = line if use_case else line.lower()
                ndl = pattern if use_case else pattern.lower()
                if ndl in hay:
                    matches.append({"line": i, "content": line})
        return {"ok": True, "path": path, "pattern": pattern,
                "regex": use_regex, "case_sensitive": use_case,
                "total_lines": len(lines), "matches": matches, "count": len(matches)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_multi_patch(path: str, patches: str, message: str, repo: str = "") -> dict:
    """Apply multiple find-replace patches via Contents API (gh_read/gh_write) — 2 HTTP calls, proven stable."""
    try:
        repo = repo or GITHUB_REPO
        if isinstance(patches, str):
            patches = json.loads(patches)
        content = gh_read(path, repo)
        applied = []
        skipped = []
        for i, patch in enumerate(patches):
            old = patch.get("old_str", "")
            new = patch.get("new_str", "")
            count = content.count(old)
            if count == 0:
                skipped.append({"index": i, "reason": "not found", "old_str": old[:60]})
            elif count > 1:
                skipped.append({"index": i, "reason": f"ambiguous ({count}x)", "old_str": old[:60]})
            else:
                content = content.replace(old, new, 1)
                applied.append({"index": i, "old_str": old[:60]})
        if not applied:
            return {"ok": False, "error": "No patches applied", "skipped": skipped, "skipped_details": skipped}
        if path.endswith(".py"):
            import py_compile, tempfile as _tmpf
            with _tmpf.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(content); tmp = tf.name
            try:
                py_compile.compile(tmp, doraise=True)
            except py_compile.PyCompileError as e:
                import os; os.unlink(tmp)
                return {"ok": False, "error": f"Syntax error (patch not pushed): {e}"}
            finally:
                import os
                if os.path.exists(tmp): os.unlink(tmp)
        ok = gh_write(path, content, message, repo)
        if not ok:
            return {"ok": False, "error": "gh_write returned False"}
        return {"ok": True, "path": path, "applied": len(applied), "skipped": len(skipped),
                "details": applied, "skipped_details": skipped}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_session_end(summary: str, actions: str, domain: str = "general",
                  patterns: str = "", quality: str = "0.8",
                  skill_file_updated: str = "false",
                  force_close: str = "false",
                  active_task_ids: str = "") -> dict:
    """One-call session close.
    skill_file_updated: TASK-21.B gate. Pass 'true' after writing new rules to local skill file.
    force_close: pass 'true' to bypass skill_file_updated gate (owner explicit override).
    active_task_ids: pipe-separated UUIDs of tasks touched this session (e.g. 'uuid1|uuid2').
      session_end checks their status and warns if any are still pending/in_progress.
      Non-blocking -- warning only, Claude decides whether to patch or leave as-is.
    Always: logs session to Supabase, runs Groq hot_reflection, scans system_map. SESSION.md is static."""
    from core_train import auto_hot_reflection
    try:
        session_start_at = datetime.utcnow()  # anchor: start of close call, used for duration
        if isinstance(actions, list):
            actions_list = [str(a).strip() for a in actions if str(a).strip()]
        else:
            # pipe-only split -- commas appear naturally inside action descriptions
            actions_list = [a.strip() for a in str(actions).split("|") if a.strip()]
        try:
            q = min(1.0, max(0.0, float(quality)))  # clamp to valid range 0.0-1.0
        except:
            q = 0.8

        # TASK-21.B: skill_file_updated gate
        # If patterns were noted this session but skill file was not confirmed written,
        # block session_end and return a warning unless force_close=true.
        _has_patterns = bool(patterns.strip())
        _skill_ok = str(skill_file_updated).strip().lower() in ("true", "1", "yes")
        _force = str(force_close).strip().lower() in ("true", "1", "yes")
        if _has_patterns and not _skill_ok and not _force:
            return {
                "ok": False,
                "blocked": True,
                "reason": "skill_file_not_updated",
                "warning": (
                    "New patterns detected but skill_file_updated=false. "
                    "Write new rules to C:\\Users\\rnvgg\\.claude-skills\\CORE_AGI_SKILL_V4.md "
                    "Section 12 via Windows-MCP:FileSystem or Desktop Commander:edit_block. "
                    "Then call session_end with skill_file_updated=true. "
                    "To skip: pass force_close=true."
                ),
                "patterns_noted": patterns,
            }

        # 1. Log session to Supabase
        session_created_at = session_start_at.isoformat()
        session_ok = sb_post("sessions", {
            "summary": summary,
            "actions": actions_list,
            "interface": "claude-desktop"
        })

        # 2. Always run Groq-powered hot reflection
        # Pass session_created_at as anchor -- auto_hot_reflection uses last hot_reflection
        # timestamp as the enrichment window lower bound, not this value directly.
        # But we pass it as fallback in case no prior hot_reflection exists.
        caller_patterns = [p.strip() for p in patterns.split("|") if p.strip()]
        r_ok = auto_hot_reflection({
            "summary": summary,
            "actions": actions_list,
            "interface": "claude-desktop",
            "domain": domain,
            "quality": q,
            "seed_patterns": caller_patterns,
            "created_at": session_created_at,
        })
        reflection_id = "logged" if r_ok else "failed"

        # 3. SESSION.md is static -- tasks live in Supabase, session log in sessions table.
        # No writes to SESSION.md: eliminates spurious Railway redeploys on every session close.
        duration_seconds = int((datetime.utcnow() - session_start_at).total_seconds())

        # 4. Scan system_map - detect drift, update volatile rows
        smap_scan = {"ok": False, "error": "skipped"}
        try:
            smap_scan = t_system_map_scan(trigger="session_end")
        except Exception as e:
            smap_scan = {"ok": False, "error": str(e)}

        # 5. Task status check -- warn if active tasks still open at close
        task_warnings = []
        if active_task_ids and active_task_ids.strip():
            ids = [i.strip() for i in active_task_ids.split("|") if i.strip()]
            for tid in ids:
                try:
                    rows = sb_get("task_queue",
                        f"select=id,task,status&id=eq.{tid}&limit=1", svc=True)
                    if rows and isinstance(rows, list) and rows[0]:
                        row = rows[0]
                        if row.get("status") in ("pending", "in_progress"):
                            task_str = str(row.get("task", ""))[:80]
                            task_warnings.append({
                                "id": tid,
                                "status": row.get("status"),
                                "task": task_str,
                                "hint": "Call sb_patch to update status before closing, or pass intentionally."
                            })
                except Exception:
                    pass

        # Build return -- surface reflection failure and task warnings
        result = {
            "ok": session_ok,
            "session_logged": session_ok,
            "reflection_logged": reflection_id,
            "training_ok": r_ok,
            "system_map_scan": smap_scan,
            "actions_count": len(actions_list),
            "duration_seconds": duration_seconds,
            "skill_file_updated": _skill_ok,
        }
        if not r_ok:
            result["reflection_warning"] = (
                "Hot reflection failed to log. Training pipeline will miss this session. "
                "Check Railway logs for [HOT] error. Common cause: Groq timeout or Supabase 400."
            )
        if task_warnings:
            result["task_status_warnings"] = task_warnings
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_core_py_rollback(commit_sha: str, file: str = "core_main.py") -> dict:
    """Emergency restore: fetch any CORE source file at a commit SHA, write back, redeploy.
    Defaults to core_main.py. core.py is deleted — do not use."""
    try:
        if not commit_sha or len(commit_sha) < 6:
            return {"ok": False, "error": "commit_sha required (min 6 chars)"}
        target = file if file else "core_main.py"
        if target == "core.py":
            return {"ok": False, "error": "core.py has been deleted. Use core_main.py or core_tools.py."}
        h = _ghh()
        ref_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{commit_sha}",
                          headers=h, timeout=10)
        ref_r.raise_for_status()
        full_sha = ref_r.json()["sha"]
        short_sha = full_sha[:12]
        file_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{target}?ref={full_sha}",
                           headers=h, timeout=30)
        file_r.raise_for_status()
        old_content = base64.b64decode(file_r.json()["content"]).decode()
        new_commit = _gh_blob_write(
            target, old_content,
            f"rollback: restore {target} from {short_sha}"
        )
        deploy = t_redeploy(f"rollback {target} to {short_sha}")
        notify_owner(f"ROLLBACK triggered — {target} restored from {short_sha}. Deploying...")
        return {
            "ok": True,
            "file": target,
            "restored_from": short_sha,
            "new_commit": new_commit[:12],
            "redeploying": deploy.get("ok", False),
            "note": "Use build_status to confirm deploy succeeds"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_diff(path: str, sha_a: str, sha_b: str = "main") -> dict:
    """Compare a file between two commits and return a unified diff."""
    try:
        h = _ghh()
        def fetch_at(ref):
            r = httpx.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={ref}",
                headers=h, timeout=20
            )
            r.raise_for_status()
            return base64.b64decode(r.json()["content"]).decode().splitlines(keepends=True)
        if sha_b == "main" or len(sha_b) < 20:
            ref_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/ref/heads/{sha_b if sha_b != 'main' else 'main'}",
                              headers=h, timeout=10)
            sha_b_full = ref_r.json()["object"]["sha"] if ref_r.is_success else sha_b
        else:
            sha_b_full = sha_b
        if sha_a == "prev":
            commit_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/commits/{sha_b_full}",
                                 headers=h, timeout=10)
            commit_r.raise_for_status()
            parents = commit_r.json().get("parents", [])
            if not parents:
                return {"ok": False, "error": "No parent commit found"}
            sha_a = parents[0]["sha"]
        lines_a = fetch_at(sha_a)
        lines_b = fetch_at(sha_b_full)
        diff = list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=f"{path}@{sha_a[:8]}",
            tofile=f"{path}@{sha_b_full[:8]}",
            n=3
        ))
        diff_text = "".join(diff)
        added   = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        return {
            "ok": True, "path": path,
            "sha_a": sha_a[:12], "sha_b": sha_b_full[:12],
            "added": added, "removed": removed,
            "diff": diff_text[:8000] if diff_text else "(no changes)"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_deploy_and_wait(reason: str = "", timeout: str = "120") -> dict:
    """DEPRECATED POLLING LOOP — replaced by build_status.
    Railway auto-deploys on every GitHub push — no polling loop needed.
    New SOP: patch_file (pushes code) → sleep 35s → build_status().
    This tool now just calls build_status() and returns immediately.
    Kept in TOOLS to avoid breaking any references.
    """
    return t_build_status()


def t_ping_health() -> dict:
    """Hit the live Railway / endpoint."""
    try:
        railway_url = os.environ.get("RAILWAY_PUBLIC_URL", "https://core-agi-production.up.railway.app")
        r = httpx.get(f"{railway_url}/", timeout=10)
        return {"ok": r.is_success, "status_code": r.status_code,
                "response": r.json() if "application/json" in r.headers.get("content-type","") else r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_verify_live(expected_text: str, timeout: str = "90") -> dict:
    """Poll /state until expected_text appears."""
    try:
        t_secs = int(timeout) if timeout else 90
        railway_url = os.environ.get("RAILWAY_PUBLIC_URL", "https://core-agi-production.up.railway.app")
        deadline = time.time() + t_secs
        poll_count = 0
        while time.time() < deadline:
            try:
                r = httpx.get(f"{railway_url}/state", timeout=8)
                if r.is_success and expected_text in r.text:
                    return {"ok": True, "found": True, "polls": poll_count,
                            "elapsed_s": round(time.time() - (deadline - t_secs))}
            except Exception:
                pass
            time.sleep(8)
            poll_count += 1
        return {"ok": False, "found": False, "polls": poll_count, "timeout_s": t_secs,
                "note": "Text not found in /state within timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Signal extraction --------------------------------------------------------
def extract_signals(task: str) -> dict:
    t = task.lower()
    intent = "generate"
    for kw, v in [("fix","fix"),("debug","fix"),("error","fix"),("broken","fix"),
                  ("explain","explain"),("what is","explain"),("how does","explain"),("teach","explain"),
                  ("find","lookup"),("search","lookup"),("who is","lookup"),("when did","lookup"),
                  ("analyze","analyze"),("review","analyze"),("check","validate"),("is this","validate"),
                  ("write","generate"),("create","generate"),("build","build"),("make","build"),
                  ("should i","decide"),("which","decide"),("recommend","decide"),
                  ("help","support"),("overwhelmed","support"),("worried","support"),("scared","support"),
                  ("plan","orchestrate"),("steps to","orchestrate"),("how to","orchestrate")]:
        if kw in t: intent = v; break
    domain = "general"
    for kw, d in [("def ","code"),("function","code"),("import ","code"),("class ","code"),("sql","code"),
                  ("contract","legal"),("liability","legal"),("clause","legal"),("lawsuit","legal"),
                  ("invoice","finance"),("revenue","finance"),("cash flow","finance"),("tax","finance"),
                  ("patient","medical"),("symptoms","medical"),("diagnosis","medical"),("medication","medical"),
                  ("marketing","business"),("customers","business"),("startup","business"),("sales","business"),
                  ("essay","academic"),("research","academic"),("thesis","academic"),("cite","academic"),
                  ("content","creative"),("story","creative"),("blog","creative"),("design","creative")]:
        if kw in t: domain = d; break
    expertise = 3
    beginner_markers = ["what is","how do i","i don't know","explain","simple","basic","beginner","noob"]
    expert_markers   = ["implement","optimize","architecture","idiomatic","edge case","tradeoff","latency","throughput","refactor"]
    if any(m in t for m in beginner_markers): expertise = 2
    if any(m in t for m in expert_markers):   expertise = 4
    if len(task.split()) <= 5 and "?" not in task: expertise = max(expertise, 4)
    emotion = "neutral"
    if any(m in t for m in ["asap","urgent","deadline","help!","tolong","buru","cepat"]): emotion = "urgent"
    elif any(m in t for m in ["still","again","doesn't work","still not"]): emotion = "frustrated"
    elif any(m in t for m in ["worried","scared","overwhelmed","anxious"]): emotion = "vulnerable"
    elif any(m in t for m in ["lol","btw","just wondering","haha"]): emotion = "casual"
    stakes = "medium"
    if any(m in t for m in ["quick","short","brief","simple","just"]): stakes = "low"
    if any(m in t for m in ["production","deploy","contract","legal","medical","critical"]): stakes = "high"
    if any(m in t for m in ["life","death","emergency"]): stakes = "critical"
    archetype_map = {
        "lookup": "A1", "explain": "A4", "generate": "A3", "fix": "A4",
        "analyze": "A4", "validate": "A8", "build": "A5", "decide": "A6",
        "orchestrate": "A7", "support": "A9",
    }
    return {"intent": intent, "domain": domain, "expertise": expertise,
            "emotion": emotion, "stakes": stakes, "archetype": archetype_map.get(intent, "A3")}



def t_ask(question: str, domain: str = ""):
    if not question: return {"ok": False, "error": "question required"}
    kb_results = t_search_kb(question, domain=domain, limit=10)
    kb_context = "\n\n".join([f"[KB: {r.get('topic','')}]\n{str(r.get('instruction') or r.get('content',''))[:600]}" for r in kb_results]) if kb_results else ""
    mistakes = t_get_mistakes(domain=domain or "general", limit=3)
    mistake_context = "\n".join([f"- Avoid: {m.get('what_failed','')} -> {m.get('correct_approach','')[:100]}" for m in mistakes]) if mistakes else ""
    system = ("You are CORE, a personal AGI assistant with accumulated knowledge from many sessions. "
              "Answer using the knowledge base context provided. Be specific and actionable.")
    user = f"Question: {question}\n\n"
    if kb_context: user += f"Relevant knowledge:\n{kb_context}\n\n"
    if mistake_context: user += f"Known pitfalls to avoid:\n{mistake_context}\n\n"
    user += "Answer:"
    model = GROQ_MODEL if (len(kb_results) > 3 or len(question) > 200) else GROQ_FAST
    try:
        answer = groq_chat(system, user, model=model, max_tokens=512)
        return {"ok": True, "answer": answer, "kb_hits": len(kb_results), "model": model, "question": question}
    except Exception as e:
        return {"ok": False, "error": str(e)}



def t_stats():
    try:
        hots = sb_get("hot_reflections", "select=domain,quality_score&limit=200", svc=True)
        domain_counts: Counter = Counter(h.get("domain","general") for h in hots)
        patterns = sb_get("pattern_frequency", "select=pattern_key,frequency,domain&stale=eq.false&order=frequency.desc&limit=10", svc=True)
        mistakes = sb_get("mistakes", "select=domain&limit=200", svc=True)
        mistake_counts: Counter = Counter(m.get("domain","general") for m in mistakes)
        scores = [min(1.0, max(0.0, float(h["quality_score"]))) for h in hots if h.get("quality_score") is not None]
        avg_quality = round(sum(scores) / len(scores), 2) if scores else None
        counts = get_system_counts()
        evo_rows = sb_get("evolution_queue", "select=status&limit=500", svc=True) or []
        evo_counts = Counter(e.get("status", "unknown") for e in evo_rows)
        cold_rows = sb_get("cold_reflections", "select=created_at&order=created_at.desc&limit=1", svc=True) or []
        last_cold = cold_rows[0].get("created_at", "never") if cold_rows else "never"
        return {
            "ok": True,
            "total_sessions": counts.get("sessions", 0),
            "knowledge_entries": counts.get("knowledge_base", 0),
            "total_mistakes": counts.get("mistakes", 0),
            "hot_reflections": len(hots),
            "avg_quality_score": avg_quality,
            "domain_distribution": dict(domain_counts.most_common(8)),
            "mistake_distribution": dict(mistake_counts.most_common(6)),
            "top_patterns": [{"pattern": p.get("pattern_key","")[:80], "freq": p.get("frequency",0), "domain": p.get("domain","")} for p in patterns],
            "evolution_queue": dict(evo_counts),
            "last_cold_processor_run": last_cold,
            "quality_trend_7d": t_get_quality_trend("7"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_search_mistakes(query: str = "", domain: str = "", limit: int = 10):
    try:
        lim = int(limit) if limit else 10
        qs = f"select=domain,context,what_failed,correct_approach,root_cause,severity&order=created_at.desc&limit={lim}"
        if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
        if query:
            q = query.strip().replace("'", "").replace('"', "")
            qs += f"&or=(what_failed.ilike.*{q}*,context.ilike.*{q}*,correct_approach.ilike.*{q}*)"
        results = sb_get("mistakes", qs, svc=True)
        return {"ok": True, "count": len(results), "mistakes": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Background Researcher (globals + helpers only — loop lives in core_train) ----
_RESEARCH_DOMAINS = [
    ("code",     ["debug this python function", "optimize SQL query", "refactor async code"]),
    ("business", ["improve cash flow", "write investor pitch", "reduce churn"]),
    ("legal",    ["draft NDA", "understand terms of service", "IP protection for startup"]),
    ("creative", ["write product description", "social media strategy", "brand voice guide"]),
    ("academic", ["summarize research paper", "explain statistical method"]),
    ("medical",  ["explain diagnosis", "medication interaction check"]),
    ("finance",  ["build financial model", "tax optimization", "runway calculation"]),
    ("data",     ["clean messy dataset", "visualize trends", "build dashboard"]),
]


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
    return new_items


def _sync_backlog_status():
    """No-op: backlog status is managed directly in the backlog table."""
    return 0


def _repopulate_evolution_queue():
    """DISABLED: Backlog items are never pushed to evolution_queue."""
    print("[RESEARCH] _repopulate_evolution_queue: disabled — backlog items never go to evolution_queue")
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


# -- KB Mining ----------------------------------------------------------------
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
        total_new = 0
        offset = 0
        batches_done = 0
        system = """You are CORE's KB mining engine. Identify gaps from KB entries.
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
        return {"ok": True, "batches_scanned": batches_done, "new_items": total_new,
                "total_backlog": final_count, "kb_count": kb_count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_list_templates(limit: int = 20) -> dict:
    try:
        rows = sb_get("script_templates",
                      f"select=name,description,trigger_pattern,use_count,created_at"
                      f"&order=use_count.desc&limit={limit}",
                      svc=True)
        return {"ok": True, "templates": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_run_template(name: str, params: str = "") -> dict:
    try:
        rows = sb_get("script_templates",
                      f"select=*&name=eq.{name}&limit=1", svc=True)
        if not rows:
            return {"ok": False, "error": f"Template '{name}' not found"}
        tpl = rows[0]
        code = tpl.get("code", "")
        if params:
            try:
                p = json.loads(params)
                for k, v in p.items():
                    code = code.replace(f"{{{k}}}", str(v))
            except Exception:
                pass
        sb_patch("script_templates", f"name=eq.{name}",
                 {"use_count": (tpl.get("use_count") or 0) + 1})
        return {
            "ok": True, "name": name,
            "description": tpl.get("description", ""),
            "trigger_pattern": tpl.get("trigger_pattern", ""),
            "code": code,
            "instruction": "Execute this code via gh_search_replace or direct MCP tool calls.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_mine_kb(max_batches: str = "50", force: str = "false") -> dict:
    """DEPRECATED 2026-03-14 - backlog table dropped. KB mining replaced by cold processor pipeline."""
    return {"ok": False, "deprecated": True, "reason": "backlog table dropped - use evolution_queue pipeline instead"}


def t_redeploy(reason: str = "") -> dict:
    """Trigger Railway redeploy via empty GitHub commit."""
    try:
        h = _ghh()
        ref = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/ref/heads/main", headers=h, timeout=10)
        ref.raise_for_status()
        current_sha = ref.json()["object"]["sha"]
        commit = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/commits/{current_sha}", headers=h, timeout=10)
        commit.raise_for_status()
        tree_sha = commit.json()["tree"]["sha"]
        msg = f"chore: trigger redeploy — {reason or 'manual trigger'}"
        new_commit = httpx.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/git/commits",
            headers=h,
            json={"message": msg, "tree": tree_sha, "parents": [current_sha]},
            timeout=15,
        )
        new_commit.raise_for_status()
        new_sha = new_commit.json()["sha"]
        update = httpx.patch(
            f"https://api.github.com/repos/{GITHUB_REPO}/git/refs/heads/main",
            headers=h,
            json={"sha": new_sha},
            timeout=15,
        )
        update.raise_for_status()
        notify(f"CORE redeploying\nReason: {reason or 'manual trigger'}\nCommit: {new_sha[:12]}")
        return {"ok": True, "reason": reason, "commit": new_sha[:12]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_logs(limit: str = "10", keyword: str = "") -> dict:
    """Fetch recent deploy history from GitHub commit log. NOTE: this is commit history, not Railway stdout.
    For live process logs, check Railway dashboard. Default limit lowered to 10 to reduce N+1 API calls."""
    try:
        lim = min(int(limit) if limit else 10, 50)
        h = _ghh()
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page={lim}", headers=h, timeout=10)
        r.raise_for_status()
        commits = r.json()
        logs = []
        kw = keyword.strip().lower() if keyword else ""
        for commit in commits[:lim]:
            sha = commit["sha"]
            msg = commit.get("commit", {}).get("message", "")[:80]
            ts  = commit.get("commit", {}).get("committer", {}).get("date", "")[:19]
            if kw and kw not in msg.lower():
                continue
            sr = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}/statuses", headers=h, timeout=8)
            statuses = sr.json() if sr.status_code == 200 else []
            railway = [s for s in statuses if "railway" in s.get("context","").lower() or "railway" in s.get("description","").lower()]
            st = railway[0] if railway else {}
            logs.append({"ts": ts, "sha": sha[:10], "message": msg,
                         "deploy": st.get("state", "no_status"), "detail": st.get("description", "")})
        latest = logs[0] if logs else {}
        return {"ok": True, "count": len(logs), "keyword": kw or "(none)",
                "latest": latest, "logs": logs,
                "note": "For live stdout logs, check Railway dashboard"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_get_backlog(status: str = "pending", limit: int = 20, min_priority: int = 1, type: str = ""):
    """DEPRECATED 2026-03-14 - backlog table dropped. Use task_queue instead."""
    return {"ok": False, "deprecated": True, "reason": "backlog table dropped - use task_queue instead"}


def t_backlog_update(title: str, status: str, result: str = ""):
    """DEPRECATED 2026-03-14 - backlog table dropped. Use task_queue instead."""
    return {"ok": False, "deprecated": True, "reason": "backlog table dropped - use task_queue instead"}


def t_changelog_add(version: str = "", component: str = "", summary: str = "",
                    before: str = "", after: str = "", change_type: str = "upgrade") -> dict:
    """Log a completed change to the changelog table + Telegram notify."""
    try:
        ts = datetime.utcnow().isoformat()
        ver = version.strip() or datetime.utcnow().strftime("v%Y%m%d")
        ok = sb_post("changelog", {
            "version":      ver,
            "change_type":  change_type.strip() or "upgrade",
            "component":    component.strip() or "general",
            "title":        summary.strip()[:120],
            "description":  summary.strip()[:500],
            "before_state": before.strip()[:300],
            "after_state":  after.strip()[:300],
            "triggered_by": "claude_desktop",
            "created_at":   ts,
        })
        if ok:
            notify(f"CHANGELOG [{ver}] {component}\n{summary[:200]}")
        return {"ok": ok, "version": ver, "component": component, "logged_at": ts}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_bulk_apply(executor_override: str = "claude_desktop", dry_run: bool = False):
    """Apply all pending evolution_queue items."""
    if isinstance(dry_run, str):
        dry_run = dry_run.strip().lower() not in ("false", "0", "no", "")
    try:
        rows = sb_get("evolution_queue",
                      "select=*&status=in.(pending,pending_desktop)&order=id.asc",
                      svc=True)
        if not rows:
            return {"ok": True, "message": "No pending evolutions", "applied": [], "total": 0}
        results = []
        for evo in rows:
            eid   = evo["id"]
            ctype = evo.get("change_type", "knowledge")
            summary = evo.get("change_summary", "")
            try:
                meta = json.loads(evo.get("diff_content") or "{}")
            except Exception:
                meta = {}
            btype    = meta.get("backlog_type", "")
            title    = meta.get("title", summary[:80])
            desc     = meta.get("description", summary)
            domain   = meta.get("domain", "general")
            original_exec = meta.get("executor", "auto")
            effective = executor_override if executor_override != "auto" else original_exec
            if dry_run:
                results.append({"id": eid, "title": title, "btype": btype,
                                 "original_executor": original_exec, "would_use": effective,
                                 "action": "dry_run - not applied"})
                continue
            if effective == "claude_desktop" or executor_override == "claude_desktop":
                if ctype == "knowledge" or btype == "new_kb":
                    ok = bool(sb_post("knowledge_base", {
                        "domain": domain, "topic": title, "content": desc,
                        "confidence": "medium", "tags": ["bulk_apply", "claude_desktop"],
                        "source": "bulk_apply",
                    }))
                    note = f"[desktop] KB entry added: {title}"
                elif btype in ("logic_improvement", "performance", "missing_data"):
                    ok = bool(sb_post("task_queue", {
                        "type": "improvement",
                        "payload": json.dumps({"title": title, "desc": desc, "domain": domain}),
                        "status": "pending", "priority": 5, "source": "bulk_apply",
                    }))
                    note = f"[desktop] Queued for execution: {title}"
                elif btype in ("new_tool", "telegram_command"):
                    ok = bool(sb_post("knowledge_base", {
                        "domain": "pending_impl", "topic": f"[TODO] {title}",
                        "content": f"Type: {btype}\n{desc}",
                        "confidence": "low", "tags": ["todo", "new_tool", "claude_desktop"],
                        "source": "bulk_apply",
                    }))
                    note = f"[desktop] Logged as TODO: {title}"
                else:
                    ok = bool(sb_post("knowledge_base", {
                        "domain": domain, "topic": title, "content": desc,
                        "confidence": "medium", "tags": ["bulk_apply"], "source": "bulk_apply",
                    }))
                    note = f"[desktop] KB fallback: {title}"
                if ok:
                    sb_patch("evolution_queue", f"id=eq.{eid}",
                             {"status": "applied", "applied_at": datetime.utcnow().isoformat()})
                results.append({"id": eid, "title": title, "ok": ok, "note": note})
            else:
                r = apply_evolution(eid)
                results.append({"id": eid, "title": title, "ok": r.get("ok"), "note": r.get("note", "")})
        applied = [r for r in results if r.get("ok")]
        failed  = [r for r in results if not r.get("ok") and not r.get("action")]
        notify(f"Bulk apply done\nApplied: {len(applied)} | Failed: {len(failed)} | Total: {len(results)}\nExecutor: {executor_override}")
        # BACKLOG.md deleted in Task 1.8 — backlog lives in Supabase only
        return {"ok": True, "applied": len(applied), "failed": len(failed), "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _gh_commit_status(sha: str = "") -> dict:
    try:
        h = _ghh()
        if not sha:
            ref = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/ref/heads/main", headers=h, timeout=10)
            ref.raise_for_status()
            sha = ref.json()["object"]["sha"]
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}/statuses", headers=h, timeout=10)
        r.raise_for_status()
        statuses = r.json()
        c = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}", headers=h, timeout=10)
        c.raise_for_status()
        commit_msg = c.json().get("commit", {}).get("message", "")[:80]
        railway = [s for s in statuses if "railway" in s.get("context", "").lower() or "railway" in s.get("description", "").lower()]
        latest = railway[0] if railway else (statuses[0] if statuses else {})
        return {
            "sha": sha[:12], "commit_msg": commit_msg,
            "state": latest.get("state", "unknown"),
            "description": latest.get("description", "no status yet"),
            "updated_at": latest.get("updated_at", ""),
            "all_statuses": [{"context": s.get("context"), "state": s.get("state"), "description": s.get("description")} for s in statuses],
        }
    except Exception as e:
        return {"state": "error", "description": str(e)}


def t_deploy_status() -> dict:
    try:
        result = _gh_commit_status()
        return {"ok": True, "commit_sha": result.get("sha", "unknown"),
                "commit_msg": result.get("commit_msg", "unknown"),
                "status": result.get("state", "unknown"),
                "description": result.get("description", ""),
                "updated_at": result.get("updated_at", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_build_status() -> dict:
    """Check last 3 commits build state. Uses _gh_commit_status() per SHA — same source as deploy_status.
    Capped at 3 commits with 8s per-request timeout and overall 30s deadline to prevent hangs."""
    try:
        h = _ghh()
        now = datetime.utcnow()
        deadline = time.time() + 30
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page=3", headers=h, timeout=8)
        r.raise_for_status()
        commits = r.json()
        deploys = []
        for commit in commits:
            if time.time() > deadline:
                break
            sha = commit["sha"]
            msg = commit.get("commit", {}).get("message", "")[:60]
            try:
                st = _gh_commit_status(sha)
                updated = st.get("updated_at", "")
                time_since = ""
                if updated:
                    try:
                        dt = datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")
                        mins = int((now - dt).total_seconds() // 60)
                        time_since = f"{mins}m ago" if mins < 60 else f"{mins//60}h{mins%60}m ago"
                    except: pass
                deploys.append({"commit_sha": sha[:12], "commit_msg": msg,
                                "state": st.get("state", "no status"),
                                "description": st.get("description", ""),
                                "updated_at": updated, "time_since": time_since})
            except Exception:
                deploys.append({"commit_sha": sha[:12], "commit_msg": msg, "state": "error fetching status"})
        latest = deploys[0] if deploys else {}
        return {"ok": True, "latest": latest, "recent": deploys,
                "summary": f"Latest: {latest.get('state','?')} — {latest.get('commit_msg','?')}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_crash_window_secs = 3600
_crash_threshold   = 2
_startup_times: list = []


def t_crash_report() -> dict:
    try:
        h = _ghh()
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page=5", headers=h, timeout=10)
        r.raise_for_status()
        commits = r.json()
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=_crash_window_secs)
        failed_recent = []
        for commit in commits:
            sha = commit["sha"]
            ts_str = commit.get("commit", {}).get("committer", {}).get("date", "")
            msg = commit.get("commit", {}).get("message", "")[:60]
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                if ts < cutoff:
                    continue
            except Exception:
                continue
            sr = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}/statuses", headers=h, timeout=8)
            statuses = sr.json() if sr.status_code == 200 else []
            railway = [s for s in statuses if "railway" in s.get("context","").lower() or "railway" in s.get("description","").lower()]
            st = railway[0] if railway else {}
            if st.get("state") == "failure":
                failed_recent.append({"sha": sha[:10], "ts": ts_str[:19], "message": msg,
                                      "detail": st.get("description", "")})
        crash_count  = len(failed_recent)
        loop_detected = crash_count > _crash_threshold
        if loop_detected:
            sb_post("mistakes", {
                "domain": "infrastructure",
                "context": f"Railway restart loop detected - {crash_count} failures in 1hr",
                "what_failed": f"Service crashed {crash_count}x in 1 hour",
                "correct_approach": "Check recent commits for syntax errors. Use t_build_status.",
                "root_cause": "Likely bad code patch deployed",
                "how_to_avoid": "Run t_build_status after every patch.",
                "severity": "critical",
                "tags": ["crash", "restart_loop", "railway"],
            })
            notify(f"CORE Restart Loop Detected\nFailures in last hour: {crash_count}\n"
                   f"Recent failed commits: {', '.join(d['sha'] for d in failed_recent[:3])}")
        summary = f"{'Restart loop: ' + str(crash_count) + ' failures' if loop_detected else 'OK - ' + str(crash_count) + ' failures'} in last hour."
        return {"ok": True, "crash_count": crash_count, "loop_detected": loop_detected,
                "threshold": _crash_threshold, "failed_recent": failed_recent, "summary": summary}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_review_evolutions() -> dict:
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "core-agi-production.up.railway.app")
    url = f"https://{railway_url}/review"
    return {"ok": True, "url": url, "note": "Open URL in browser to review pending evolutions."}


# -- Project Mode tools -------------------------------------------------------

def t_project_list() -> dict:
    """List all registered projects from Supabase."""
    try:
        rows = sb_get("projects", "select=project_id,name,status,last_indexed,folder_path&order=created_at.asc", svc=True) or []
        return {"ok": True, "projects": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_get(project_ids: str = "") -> dict:
    """Load full context for one or more projects. project_ids = comma-separated slugs or list."""
    try:
        def _extract_id(p):
            if isinstance(p, dict):
                return str(p.get("id") or p.get("project_id") or next(iter(p.values()), "")).strip()
            return str(p).strip()
        if isinstance(project_ids, list):
            ids = [_extract_id(p) for p in project_ids if _extract_id(p)]
        else:
            ids = [p.strip() for p in str(project_ids).split(",") if p.strip()]
        if not ids:
            return {"ok": False, "error": "project_ids required"}
        results = []
        for pid in ids:
            # Try unconsumed prepared context first
            ctx_rows = sb_get("project_context",
                f"select=context_md,id&project_id=eq.{pid}&consumed=eq.false&order=prepared_at.desc&limit=1",
                svc=True) or []
            if ctx_rows:
                ctx = ctx_rows[0].get("context_md", "")
            else:
                # Fall back to top 30 KB entries
                kb = sb_get("knowledge_base",
                    f"select=topic,instruction,content&domain=eq.project%3A{pid}&order=updated_at.desc&limit=30",
                    svc=True) or []
                ctx = "\n\n".join([
                    f"### {r['topic']}\n" +
                    (f"**Directive:** {r['instruction']}\n" if r.get('instruction') else "") +
                    r.get('content', '')
                    for r in kb
                ])
            results.append({"project_id": pid, "context": ctx})
        return {"ok": True, "results": results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_search(project_id: str = "", query: str = "") -> dict:
    """Search KB entries for a specific project. Server-side ilike on topic+content."""
    try:
        if not project_id or not query:
            return {"ok": False, "error": "project_id and query required"}
        domain = f"project:{project_id}"
        enc_q = query.replace("%", "%25")
        rows = sb_get("knowledge_base",
            f"select=topic,content,confidence&domain=eq.{domain}&or=(topic.ilike.*{enc_q}*,content.ilike.*{enc_q}*)&limit=30",
            svc=True) or []
        return {"ok": True, "project_id": project_id, "query": query, "hits": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_context_check() -> dict:
    """Check for unconsumed prepared project contexts (Telegram-prepared, waiting for Desktop)."""
    try:
        rows = sb_get("project_context",
            "select=project_id,prepared_by,prepared_at&consumed=eq.false&order=prepared_at.desc",
            svc=True) or []
        return {"ok": True, "pending": rows, "count": len(rows)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_register(project_id: str = "", name: str = "", folder_path: str = "", index_path: str = "") -> dict:
    """Register a new project in Supabase."""
    try:
        if not project_id or not name:
            return {"ok": False, "error": "project_id and name required"}
        ok = sb_post_critical("projects", {
            "project_id": project_id,
            "name": name,
            "folder_path": folder_path,
            "index_path": index_path,
            "status": "active",
        })
        if ok:
            return {"ok": True, "project_id": project_id, "name": name}
        return {"ok": False, "error": "insert failed"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_update_kb(project_id: str = "", topic: str = "", content: str = "", confidence: str = "high") -> dict:
    """Add or update a KB entry for a project. domain=project:{project_id}."""
    try:
        if not project_id or not topic or not content:
            return {"ok": False, "error": "project_id, topic, content required"}
        domain = f"project:{project_id}"
        ok = sb_upsert("knowledge_base",
            {"domain": domain, "topic": topic, "content": content, "confidence": confidence},
            on_conflict="domain,topic")
        return {"ok": bool(ok), "domain": domain, "topic": topic}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_update_index(project_id: str = "", last_indexed: str = "") -> dict:
    """Update last_indexed timestamp for a project in Supabase."""
    try:
        if not project_id:
            return {"ok": False, "error": "project_id required"}
        ts = last_indexed or datetime.utcnow().isoformat()
        ok = sb_patch("projects", f"project_id=eq.{project_id}", {"last_indexed": ts})
        return {"ok": bool(ok), "project_id": project_id, "last_indexed": ts}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_prepare(project_ids: str = "") -> dict:
    """Railway-side: assemble context for project(s) and store in project_context for Desktop to consume."""
    try:
        def _extract_id(p):
            if isinstance(p, dict):
                return str(p.get("id") or p.get("project_id") or next(iter(p.values()), "")).strip()
            return str(p).strip()
        if isinstance(project_ids, list):
            ids = [_extract_id(p) for p in project_ids if _extract_id(p)]
        else:
            ids = [p.strip() for p in str(project_ids).split(",") if p.strip()]
        if not ids:
            return {"ok": False, "error": "project_ids required"}
        prepared = []
        for pid in ids:
            proj_rows = sb_get("projects", f"select=name&project_id=eq.{pid}&limit=1", svc=True) or []
            if not proj_rows:
                continue
            name = proj_rows[0].get("name", pid)
            kb = sb_get("knowledge_base",
                f"select=topic,instruction,content&domain=eq.project%3A{pid}&order=updated_at.desc&limit=30",
                svc=True) or []
            context_md = f"# Project Context: {name}\n\n"
            context_md += "\n\n".join([
                f"### {r['topic']}\n" +
                (f"**Directive:** {r['instruction']}\n" if r.get('instruction') else "") +
                r.get('content', '')
                for r in kb
            ])
            sb_post_critical("project_context", {
                "project_id": pid,
                "prepared_by": "railway",
                "context_md": context_md,
                "consumed": False,
            })
            notify(f"Project ready: {name}. Open Claude Desktop to activate.")
            prepared.append(pid)
        return {"ok": True, "prepared": prepared, "count": len(prepared)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_project_consume(project_id: str = "") -> dict:
    """Mark project_context rows as consumed after Claude Desktop has loaded them."""
    try:
        if not project_id:
            return {"ok": False, "error": "project_id required"}
        ts = datetime.utcnow().isoformat()
        ok = sb_patch("project_context",
            f"project_id=eq.{project_id}&consumed=eq.false",
            {"consumed": True, "consumed_at": ts})
        return {"ok": bool(ok), "project_id": project_id, "consumed_at": ts}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- synthesize_evolutions ----------------------------------------------------
def t_synthesize_evolutions() -> dict:
    """Pure signal fetcher for Claude Desktop architect synthesis.

    Groq's job ends at cold_processor (pattern clustering + queuing evolutions).
    Claude (in Desktop chat) is the architect -- NOT Groq.

    This tool collects all accumulated signals and returns them to Claude.
    Claude then:
      - Reads all signals directly in the chat
      - Thinks 6 months ahead as unconstrained architect
      - Calls task_add for each new task
      - Registers tasks into task_queue (source=core_v6_registry)

    No Groq call. No auto task insertion. No Telegram notify.
    The blueprint and task_add calls are 100% Claude's responsibility.
    """
    try:
        # 1. All pending evolutions
        evolutions = sb_get("evolution_queue",
            "select=id,change_type,change_summary,pattern_key,confidence,impact&status=eq.pending&order=confidence.desc",
            svc=True) or []

        # 2. Top patterns by frequency (top 40) -- exclude stale dead patterns
        patterns = sb_get("pattern_frequency",
            "select=pattern_key,frequency,domain&stale=eq.false&order=frequency.desc&limit=40",
            svc=True) or []

        # 3. Recent cold_reflections (last 10)
        cold = sb_get("cold_reflections",
            "select=summary_text,patterns_found,evolutions_queued,created_at&order=id.desc&limit=10",
            svc=True) or []

        # 4. Hot reflection gaps (last 20)
        gaps = sb_get("hot_reflections",
            "select=gaps_identified,domain,quality_score&gaps_identified=not.is.null&order=id.desc&limit=20",
            svc=True) or []

        # 5. Open task_queue items (avoid duplicating)
        open_tasks = sb_get("task_queue",
            "select=task&status=eq.pending&source=eq.core_v6_registry&order=id.desc&limit=20",
            svc=True) or []

        # Return all raw signals to Claude Desktop.
        # Claude reads, reasons as architect, calls task_add directly.
        return {
            "ok": True,
            "instruction": "YOU are the architect. Read all signals. Think 6 months ahead. Call task_add for each new task. Do NOT duplicate open_tasks. Subtasks must be concrete and executable.",
            "signals": {
                "pending_evolutions": [
                    {
                        "id": e.get("id"),
                        "confidence": e.get("confidence"),
                        "change_type": e.get("change_type"),
                        "domain": e.get("impact"),
                        "pattern_key": e.get("pattern_key", "")[:200],
                        "summary": e.get("change_summary", "")[:300],
                    } for e in evolutions[:50]
                ],
                "top_patterns": [
                    {
                        "pattern": p.get("pattern_key", "")[:200],
                        "frequency": p.get("frequency"),
                        "domain": p.get("domain"),
                    } for p in patterns
                ],
                "cold_reflection_themes": [
                    {
                        "date": c.get("created_at", "")[:10],
                        "summary": c.get("summary_text", "")[:300],
                        "patterns_found": c.get("patterns_found"),
                        "evolutions_queued": c.get("evolutions_queued"),
                    } for c in cold
                ],
                "hot_gaps": [
                    {
                        "domain": g.get("domain"),
                        "quality": g.get("quality_score"),
                        "gaps": g.get("gaps_identified"),
                    } for g in gaps
                ],
                "open_tasks": [
                    str(t.get("task", ""))[:150] for t in open_tasks
                ],
            },
            "counts": {
                "evolutions": len(evolutions),
                "patterns": len(patterns),
                "cold_reflections": len(cold),
                "gaps": len(gaps),
                "open_tasks": len(open_tasks),
            },
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}



# -- Task 8: Server-side patching tools ---------------------------------------

def t_patch_file(path: str, patches: str, message: str, repo: str = "", dry_run: str = "false") -> dict:
    """Server-side patch: fetch file from GitHub, apply find-replace patches,
    run py_compile if .py, then push. Prevents syntax errors from crashing Railway.
    patches: JSON array of {old_str, new_str} objects (same format as multi_patch).
    dry_run: true = show diff but do not push."""
    try:
        import subprocess, tempfile as _tmpfile
        repo = repo or GITHUB_REPO
        if isinstance(patches, str):
            patches = json.loads(patches)
        content = gh_read(path, repo)
        applied = []
        skipped = []
        for i, patch in enumerate(patches):
            old = patch.get("old_str", "")
            new = patch.get("new_str", "")
            count = content.count(old)
            if count == 0:
                skipped.append({"index": i, "reason": "not found", "old_str": old[:60]})
            elif count > 1:
                skipped.append({"index": i, "reason": f"ambiguous ({count}x)", "old_str": old[:60]})
            else:
                content = content.replace(old, new, 1)
                applied.append({"index": i, "old_str": old[:60]})
        if not applied:
            return {"ok": False, "error": "No patches applied", "skipped": skipped}
        # Syntax check for .py files
        syntax_ok = True
        syntax_error = ""
        if path.endswith(".py"):
            with _tmpfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tf:
                tf.write(content)
                tf_path = tf.name
            try:
                r = subprocess.run(
                    ["python3", "-m", "py_compile", tf_path],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode != 0:
                    syntax_ok = False
                    syntax_error = r.stderr.strip().replace(tf_path, path)
            finally:
                try:
                    os.unlink(tf_path)
                except Exception:
                    pass
            if not syntax_ok:
                return {"ok": False, "error": f"Syntax error - NOT pushed: {syntax_error}",
                        "applied": len(applied), "skipped": len(skipped)}
        if str(dry_run).lower() == "true":
            return {"ok": True, "dry_run": True, "path": path,
                    "applied": len(applied), "skipped": len(skipped),
                    "syntax_ok": syntax_ok, "details": applied, "skipped_details": skipped}
        ok = gh_write(path, content, message, repo)
        if not ok:
            return {"ok": False, "error": "gh_write returned False"}
        return {"ok": True, "dry_run": False, "path": path,
                "applied": len(applied), "skipped": len(skipped),
                "syntax_ok": syntax_ok, "details": applied, "skipped_details": skipped}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_validate_syntax(path: str, repo: str = "") -> dict:
    """Fetch a .py file from GitHub and run py_compile on it server-side.
    Returns ok=True/False, error line number and message if syntax error found.
    Use before any deploy to catch issues without pushing."""
    try:
        import py_compile, tempfile as _tmpfile
        if not path.endswith(".py"):
            return {"ok": True, "skipped": True, "reason": "Not a .py file"}
        content = gh_read(path, repo or GITHUB_REPO)
        with _tmpfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
            tf.write(content)
            tf_path = tf.name
        try:
            py_compile.compile(tf_path, doraise=True)
            return {"ok": True, "path": path, "lines": len(content.splitlines()),
                    "size_kb": round(len(content.encode()) / 1024, 1), "message": "Syntax OK"}
        except py_compile.PyCompileError as e:
            err_msg = str(e).replace(tf_path, path)
            return {"ok": False, "path": path, "syntax_error": err_msg}
        finally:
            try:
                os.unlink(tf_path)
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_append_to_file(path: str, content_to_append: str, message: str, repo: str = "") -> dict:
    """Fetch a file from GitHub, append content, run py_compile if .py, push.
    Designed for adding new functions without fetching the whole file into Claude context.
    content_to_append: the text to append (must include leading newlines as needed)."""
    try:
        import subprocess, tempfile as _tmpfile
        repo = repo or GITHUB_REPO
        existing = gh_read(path, repo)
        new_content = existing + content_to_append
        # Syntax check for .py files
        if path.endswith(".py"):
            with _tmpfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tf:
                tf.write(new_content)
                tf_path = tf.name
            try:
                r = subprocess.run(
                    ["python3", "-m", "py_compile", tf_path],
                    capture_output=True, text=True, timeout=15
                )
                if r.returncode != 0:
                    err_msg = r.stderr.strip().replace(tf_path, path)
                    return {"ok": False, "error": f"Syntax error - NOT pushed: {err_msg}"}
            finally:
                try:
                    os.unlink(tf_path)
                except Exception:
                    pass
        ok = gh_write(path, new_content, message, repo)
        if not ok:
            return {"ok": False, "error": "gh_write returned False"}
        return {"ok": True, "path": path,
                "original_lines": len(existing.splitlines()),
                "appended_lines": len(content_to_append.splitlines()),
                "total_lines": len(new_content.splitlines())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Supabase write layer (TASK-14) ------------------------------------------
def t_sb_patch(table: str, filters: str, data) -> dict:
    """Update rows in a Supabase table matching filters.
    filters: PostgREST filter string e.g. 'id=eq.abc123'. REQUIRED.
    data: JSON string or dict of fields to update e.g. '{"status": "done"}'.
    Never call without filters -- full-table updates are blocked."""
    if not filters or not filters.strip():
        return {"ok": False, "error": "BLOCKED: filters required -- cannot update entire table"}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            return {"ok": False, "error": f"data must be valid JSON: {e}"}
    if not isinstance(data, dict) or not data:
        return {"ok": False, "error": "data must be a non-empty JSON object"}
    ok = sb_patch(table, filters.strip(), data)
    return {"ok": ok, "table": table, "filters": filters, "updated_fields": list(data.keys())}


def t_sb_upsert(table: str, data, on_conflict: str) -> dict:
    """Insert a row or update it if it already exists (upsert).
    data: JSON string or dict of the full row.
    on_conflict: column(s) defining uniqueness e.g. 'domain,topic' for knowledge_base,
                 'project_id' for projects, 'name' for script_templates.
    Common on_conflict values: knowledge_base=domain,topic -- projects=project_id -- script_templates=name"""
    if not on_conflict or not on_conflict.strip():
        return {"ok": False, "error": "on_conflict required -- specify column(s) e.g. 'domain,topic'"}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception as e:
            return {"ok": False, "error": f"data must be valid JSON: {e}"}
    if not isinstance(data, dict) or not data:
        return {"ok": False, "error": "data must be a non-empty JSON object"}
    ok = sb_upsert(table, data, on_conflict.strip())
    return {"ok": ok, "table": table, "on_conflict": on_conflict, "fields": list(data.keys())}


def t_sb_delete(table: str, filters: str, confirm: str = "") -> dict:
    """Delete rows from a Supabase table matching filters.
    filters: PostgREST filter string e.g. 'id=eq.abc123'. REQUIRED -- rejected if empty.
    confirm: pass the literal string 'DELETE' to execute. Any other value runs a dry-run
             showing rows that WOULD be deleted without touching anything.
    PROTECTED tables (cannot delete from): sessions, mistakes, hot_reflections,
    cold_reflections, pattern_frequency, changelog, evolution_queue.
    ALLOWED tables: knowledge_base, task_queue, project_context, script_templates,
    system_map, projects.
    Always dry-run first to see what will be affected."""
    _PROTECTED = {
        "sessions", "mistakes", "hot_reflections", "cold_reflections",
        "pattern_frequency", "changelog", "evolution_queue"
    }
    _ALLOWED = {
        "knowledge_base", "task_queue", "project_context",
        "script_templates", "system_map", "projects"
    }
    if not filters or not str(filters).strip():
        return {"ok": False, "error": "BLOCKED: filters required -- cannot delete from entire table"}
    if table in _PROTECTED:
        return {"ok": False, "error": f"BLOCKED: {table} is a protected audit table -- deletes not allowed"}
    if table not in _ALLOWED:
        return {"ok": False, "error": f"BLOCKED: {table} not in allowed list. Allowed: {sorted(_ALLOWED)}"}
    # Dry-run: preview rows that would be deleted
    if str(confirm).strip() != "DELETE":
        try:
            preview = sb_get(table, f"{filters}&limit=10", svc=True)
            return {
                "ok": True,
                "dry_run": True,
                "table": table,
                "filters": filters,
                "would_delete_preview": preview,
                "row_count_estimate": len(preview),
                "message": "Dry run -- pass confirm='DELETE' to execute"
            }
        except Exception as e:
            return {"ok": False, "error": f"dry-run preview failed: {e}"}
    # Execute delete
    ok = sb_delete(table, filters.strip())
    return {"ok": ok, "table": table, "filters": filters, "deleted": ok}


# -- 13.F New tools -----------------------------------------------------------

def t_get_state_key(key: str) -> dict:
    """Read back a specific state key written by update_state. Fills the read-back gap."""
    if not key:
        return {"ok": False, "error": "key required"}
    try:
        rows = sb_get("sessions",
            f"select=summary&summary=like.[state_update] {key}:%&order=id.desc&limit=1",
            svc=True) or []
        if not rows:
            return {"ok": False, "key": key, "found": False, "value": None}
        raw = rows[0].get("summary", "")
        prefix = f"[state_update] {key}: "
        value = raw[len(prefix):].strip() if raw.startswith(prefix) else raw
        return {"ok": True, "key": key, "value": value, "found": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_task_update(task_id: str, status: str, result: str = "") -> dict:
    """Update a task_queue row status. task_id = UUID or TASK-N string. status = pending/in_progress/done/failed."""
    valid = {"pending", "in_progress", "done", "failed"}
    if not task_id or not status:
        return {"ok": False, "error": "task_id and status required"}
    if status not in valid:
        return {"ok": False, "error": f"status must be one of: {valid}"}
    try:
        # Try UUID match first (validate UUID format before querying)
        import re as _re
        _is_uuid = bool(_re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', task_id.lower()))
        rows = []
        if _is_uuid:
            rows = sb_get("task_queue", f"select=id,task,status&id=eq.{task_id}&limit=1", svc=True) or []
        if not rows:
            # Fall back: search task JSON for task_id field (handles TASK-N strings)
            all_rows = sb_get(
                "task_queue",
                f"select=id,task,status&source=in.(core_v6_registry,mcp_session)&limit=200",
                svc=True
            ) or []
            rows = [r for r in all_rows if f'"task_id": "{task_id}"' in str(r.get("task", ""))
                    or f'"title": "{task_id}"' in str(r.get("task", ""))]
        if not rows:
            return {"ok": False, "error": f"task not found: {task_id}"}
        row_id = rows[0]["id"]
        data = {"status": status}
        if result:
            data["result"] = result
        ok = sb_patch("task_queue", f"id=eq.{row_id}", data)
        return {"ok": ok, "task_id": task_id, "row_id": row_id, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_task_add(title: str, description: str = "", priority: str = "5",
               subtasks: str = "", blocked_by: str = "") -> dict:
    """Add a new task to task_queue with proper schema. source=mcp_session set automatically."""
    if not title:
        return {"ok": False, "error": "title required"}
    try:
        pri = int(priority) if priority else 5
    except Exception:
        pri = 5
    task_json = json.dumps({
        "title": title,
        "description": description,
        **({"subtasks": subtasks} if subtasks else {}),
        **({"blocked_by": blocked_by} if blocked_by else {}),
    })
    try:
        ok = sb_post("task_queue", {
            "task": task_json,
            "status": "pending",
            "priority": pri,
            "source": "mcp_session",
        })
        return {"ok": ok, "title": title, "priority": pri}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_kb_update(domain: str, topic: str, instruction: str = "",
                content: str = "", confidence: str = "medium") -> dict:
    """Upsert a KB entry on domain+topic. Updates if exists, inserts if new. Prevents duplicates.
    Use instead of add_knowledge when the rule may already exist."""
    if not domain or not topic:
        return {"ok": False, "error": "domain and topic required"}
    if not instruction and not content:
        return {"ok": False, "error": "at least one of instruction or content required"}
    try:
        ok = sb_upsert("knowledge_base", {
            "domain": domain, "topic": topic,
            "instruction": instruction or None,
            "content": content or "",
            "confidence": confidence,
            "source": "mcp_session",
        }, "domain,topic")
        return {"ok": ok, "domain": domain, "topic": topic, "action": "upserted"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_mistakes_since(hours: str = "24") -> dict:
    """Return mistakes logged in the last N hours. Use at session_end to see only this session's errors."""
    try:
        h = int(hours) if hours else 24
        rows = sb_get("mistakes",
            f"select=domain,context,what_failed,correct_approach,severity,root_cause,how_to_avoid"
            f"&created_at=gte.now()-interval.{h}.hours&order=created_at.desc&limit=50",
            svc=True) or []
        return {"ok": True, "hours": h, "count": len(rows), "mistakes": rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- TASK-21: Persistent Evolution Engine ------------------------------------
def t_add_evolution_rule(
    rule: str,
    domain: str,
    category: str = "hard_rule",
    source: str = "session_correction",
) -> dict:
    """Write a new behavioral rule to ALL server-side persistence layers atomically.

    RULE #1 FOR AGI: evolution = data in storage, not chat promises.
    This tool writes to:
      1. knowledge_base (instruction field, confidence=proven, tags=[evolution_rule])
      2. SESSION.md Active Rules table (gh_search_replace append)

    NOTE: Cannot write to local skill file (C:\\Users\\rnvgg\\.claude-skills\\CORE_AGI_SKILL_V4.md)
    because this runs on Railway. That write is Claude Desktop's job via Windows-MCP:FileSystem.
    This tool returns a reminder. session_end gate (TASK-21.B) enforces it.

    category: hard_rule | sop | architectural_decision | correction
    source: session_correction | owner_directive | cold_processor
    """
    try:
        if not rule or not domain:
            return {"ok": False, "error": "rule and domain are required"}

        persisted_to = []

        # 1 -- Write to knowledge_base
        kb_ok = sb_upsert("knowledge_base", {
            "domain": domain,
            "topic": f"evolution_rule: {rule[:80]}",
            "instruction": rule,
            "content": f"category={category} source={source}. Established via add_evolution_rule. Persists across all sessions.",
            "confidence": "proven",
            "source": "evolution_rule",
            "tags": ["evolution_rule", "persistent", category],
        }, "domain,topic")
        if kb_ok:
            persisted_to.append("knowledge_base")

        # 2 -- Write to SESSION.md Active Rules table
        try:
            session_md = gh_read("SESSION.md")
            # Find the Active Rules table and append a new row
            rule_short = rule[:120].replace("|", "-").replace("\n", " ")
            new_row = f"| `{category}` | {rule_short} |"
            if "| Rule | Detail |" in session_md:
                # Append after the last rule row in the table
                old_anchor = "| `deploy_and_wait is DEPRECATED`"
                if old_anchor not in session_md:
                    # Generic append: find table end and insert
                    session_md_updated = session_md.replace(
                        "\n\n## ",
                        f"\n{new_row}\n\n## ",
                        1
                    )
                else:
                    session_md_updated = session_md  # fallback: no change
            else:
                session_md_updated = session_md  # table not found, skip

            if session_md_updated != session_md:
                gh_write("SESSION.md", session_md_updated,
                         f"evolution_rule: {rule[:60]} [skip ci]")
                persisted_to.append("session_md")
        except Exception as _se:
            print(f"[EVO_RULE] SESSION.md write failed: {_se}")

        return {
            "ok": True,
            "persisted_to": persisted_to,
            "rule": rule[:120],
            "domain": domain,
            "category": category,
            "reminder": (
                "IMPORTANT: Now write this rule to the LOCAL SKILL FILE: "
                "C:\\Users\\rnvgg\\\.claude-skills\\CORE_AGI_SKILL_V4.md "
                "Section 12 via Windows-MCP:FileSystem or Desktop Commander:edit_block. "
                "Then call session_end."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_get_quality_trend(days: str = "7") -> dict:
    """Return session quality trend for the last N days.
    Shows daily average quality score, overall trend direction, and best/worst day."""
    try:
        d = int(days) if days else 7
        cutoff = (datetime.utcnow() - timedelta(days=d)).isoformat()
        rows = sb_get(
            "hot_reflections",
            f"select=quality_score,created_at,domain,source&quality_score=not.is.null"
            f"&quality_score=lte.1.0&source=eq.real&created_at=gte.{cutoff}&order=created_at.asc",
            svc=True
        ) or []
        if not rows:
            return {"ok": True, "days": d, "entries": 0, "trend": "no_data",
                    "daily": [], "avg": None, "best_day": None, "worst_day": None}

        # Group by day
        daily: dict = {}
        for r in rows:
            day = r.get("created_at", "")[:10]
            score = float(r.get("quality_score", 0))
            if day not in daily:
                daily[day] = []
            daily[day].append(score)

        daily_avgs = [
            {"date": day, "avg": round(sum(scores) / len(scores), 3), "count": len(scores)}
            for day, scores in sorted(daily.items())
        ]

        all_scores = [r["avg"] for r in daily_avgs]
        overall_avg = round(sum(all_scores) / len(all_scores), 3)

        # Trend: compare first half vs second half
        mid = len(all_scores) // 2
        if mid > 0 and len(all_scores) >= 2:
            first_half = sum(all_scores[:mid]) / mid
            second_half = sum(all_scores[mid:]) / len(all_scores[mid:])
            if second_half - first_half > 0.03:
                trend = "improving"
            elif first_half - second_half > 0.03:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"

        best = max(daily_avgs, key=lambda x: x["avg"])
        worst = min(daily_avgs, key=lambda x: x["avg"])

        return {
            "ok": True,
            "days": d,
            "entries": len(rows),
            "overall_avg": overall_avg,
            "trend": trend,
            "daily": daily_avgs,
            "best_day": best,
            "worst_day": worst,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Tool registry ------------------------------------------------------------
TOOLS = {
    "get_state":              {"fn": t_state,                  "perm": "READ",    "args": [],
                               "desc": "Get current CORE state: last session, counts, in_progress+pending tasks. session_md=full SESSION.md content (static bootstrap doc). Pass include_operating_context=true to also load operating_context.json."},
    "get_system_health":      {"fn": t_health,                 "perm": "READ",    "args": [],
                               "desc": "Check health of all components: Supabase, Groq, Telegram, GitHub"},
    "get_constitution":       {"fn": t_constitution,           "perm": "READ",    "args": [],
                               "desc": "Get CORE immutable constitution"},
    "get_quality_trend":      {"fn": t_get_quality_trend,      "perm": "READ",    "args": ["days"],
                               "desc": "TASK-9.C: Session quality trend for last N days (default 7). Returns daily avg scores, overall trend (improving/stable/declining), best/worst day. Data sourced from hot_reflections quality_score field."},
    "add_evolution_rule":     {"fn": t_add_evolution_rule,     "perm": "WRITE",   "args": ["rule", "domain", "category", "source"],
                               "desc": "TASK-21: Persist a new behavioral rule to KB + SESSION.md atomically. Call when any new hard rule, SOP, correction or architectural decision is established this session. Returns reminder to also write to local skill file via Windows-MCP:FileSystem. category=hard_rule|sop|architectural_decision|correction."},
    "get_training_status":    {"fn": t_training_status,        "perm": "READ",    "args": [],
                               "desc": "Get training pipeline status: unprocessed hot, pending evolutions, thresholds"},
    "search_kb":              {"fn": t_search_kb,              "perm": "READ",    "args": ["query", "domain", "limit"],
                               "desc": "Search knowledge base by query (multi-word, searches topic+instruction+content) and optional domain. Returns domain, topic, instruction, content, confidence. Use before any write to check if knowledge already exists."},
    "get_mistakes":           {"fn": t_get_mistakes,           "perm": "READ",    "args": ["domain", "limit"],
                               "desc": "Get recorded mistakes with full fields: context, what_failed, correct_approach, severity, root_cause, how_to_avoid. Call before any write op in a domain to avoid repeating known errors."},
    "read_file":              {"fn": t_read_file,              "perm": "READ",    "args": ["path", "repo", "start_line", "end_line"],
                               "desc": "Read file from GitHub repo. Optional start_line/end_line for range. Returns total_line_count + truncated flag. Cap 8000 chars. For large files use gh_read_lines instead."},
    "sb_query":               {"fn": t_sb_query,               "perm": "READ",    "args": ["table", "filters", "limit", "order", "select"],
                               "desc": "Raw Supabase query. table=table name. filters=PostgREST filter string (e.g. status=eq.pending). limit=row count (default 20). order=sort column (e.g. created_at.desc). select=columns (default *). Use dedicated tools first (search_kb, get_mistakes etc) -- this is the escape hatch for queries those tools cannot handle."},
    "list_evolutions":        {"fn": t_list_evolutions,        "perm": "READ",    "args": ["status"],
                               "desc": "List evolutions. status=pending|synthesized|applied|rejected (default: pending). Use synthesized to see items Claude has already read via synthesize_evolutions."},
    "update_state":           {"fn": t_update_state,           "perm": "WRITE",   "args": ["key", "value", "reason"],
                               "desc": "Write a key-value state update to sessions table. Use get_state_key to read it back. Useful for persisting simulation settings or mid-session flags across tool calls."},
    "set_simulation":         {"fn": t_set_simulation,         "perm": "WRITE",   "args": ["instruction"],
                               "desc": "Set a custom simulation scenario for the background researcher. CORE crafts the Groq prompt and loops it every 60 min. Empty instruction resets to default."},
    "add_knowledge":          {"fn": t_add_knowledge,          "perm": "WRITE",   "args": ["domain", "topic", "instruction", "content", "tags", "confidence"],
                               "desc": "Add entry to knowledge base. instruction=behavioral directive for CORE (primary — what CORE should DO). content=supporting detail (optional). At least one of instruction/content required. Use kb_update instead if the entry may already exist."},
    "log_mistake":            {"fn": t_log_mistake,            "perm": "WRITE",   "args": ["context", "what_failed", "correct_approach", "domain", "root_cause", "how_to_avoid", "severity"],
                               "desc": "Log a mistake so CORE never repeats it. correct_approach=the right way to do it (required). severity=low|medium|high|critical. Always call this when CORE makes an error — it is the primary learning mechanism."},
    "notify_owner":           {"fn": t_notify,                 "perm": "WRITE",   "args": ["message", "level"],
                               "desc": "Send Telegram notification."},
    "sb_insert":              {"fn": t_sb_insert,              "perm": "WRITE",   "args": ["table", "data"],
                               "desc": "Insert row into Supabase table."},
    "sb_bulk_insert":         {"fn": t_sb_bulk_insert,         "perm": "WRITE",   "args": ["table", "rows"],
                               "desc": "Insert multiple rows into Supabase in one HTTP call."},
    "get_state_key":          {"fn": t_get_state_key,          "perm": "READ",    "args": ["key"],
                               "desc": "Read back a specific state key written by update_state. Use to retrieve values set via update_state or set_simulation — the only way to read back those keys."},
    "task_update":            {"fn": t_task_update,            "perm": "WRITE",   "args": ["task_id", "status", "result"],
                               "desc": "Update a task_queue row status. task_id=UUID or TASK-N string. status=pending/in_progress/done/failed. result=optional outcome note. Use instead of raw sb_query for task status changes."},
    "task_add":               {"fn": t_task_add,               "perm": "WRITE",   "args": ["title", "description", "priority", "subtasks", "blocked_by"],
                               "desc": "Add a new task to task_queue with proper schema. Sets source=mcp_session automatically. Use instead of raw sb_insert for new tasks — enforces correct structure."},
    "kb_update":              {"fn": t_kb_update,              "perm": "WRITE",   "args": ["domain", "topic", "instruction", "content", "confidence"],
                               "desc": "Upsert a KB entry on domain+topic conflict. Updates if exists, inserts if new. Use instead of add_knowledge when the rule may already exist — prevents duplicates."},
    "mistakes_since":         {"fn": t_mistakes_since,         "perm": "READ",    "args": ["hours"],
                               "desc": "Return mistakes logged in the last N hours (default 24). Use at session_end to review only this session's errors, not the rolling last-10."},
    "trigger_cold_processor": {"fn": t_trigger_cold_processor, "perm": "WRITE",   "args": [],
                               "desc": "Manually trigger cold processor."},
    "approve_evolution":      {"fn": t_approve_evolution,      "perm": "WRITE",   "args": ["evolution_id"],
                               "desc": "Approve and apply a pending evolution by ID"},
    "reject_evolution":       {"fn": t_reject_evolution,       "perm": "WRITE",   "args": ["evolution_id", "reason"],
                               "desc": "Reject a pending evolution by ID."},
    "bulk_reject_evolutions": {"fn": t_bulk_reject_evolutions, "perm": "WRITE",   "args": ["change_type", "ids", "reason", "include_synthesized"],
                               "desc": "Bulk reject pending evolutions silently. change_type=backlog|knowledge|empty, or comma-separated ids. include_synthesized=true to also reject synthesized items."},
    "gh_search_replace":      {"fn": t_gh_search_replace,      "perm": "EXECUTE", "args": ["path", "old_str", "new_str", "message", "repo", "dry_run"],
                               "desc": "Surgical find-and-replace in a GitHub file. old_str must be unique in the file. Use for non-.py files or small targeted edits. For .py files prefer patch_file (has py_compile guard). Always read the target lines first with gh_read_lines."},
    "gh_read_lines":          {"fn": t_gh_read_lines,          "perm": "READ",    "args": ["path", "start_line", "end_line", "repo"],
                               "desc": "Read specific line range from a GitHub file with line numbers. Use before any edit to get exact content. Preferred over read_file for large files or when you need a specific section."},
    "write_file":             {"fn": t_write_file,             "perm": "EXECUTE", "args": ["path", "content", "message", "repo"],
                               "desc": "Write NEW file to GitHub repo — FULL OVERWRITE. BLOCKED for core_main.py and core_tools.py. Use patch_file or gh_search_replace for surgical edits on existing files. Only use for brand new files."},
    "ask":                    {"fn": t_ask,                    "perm": "READ",    "args": ["question", "domain"],
                               "desc": "Ask CORE anything — searches KB + mistakes for context, then answers via Groq. Use for domain questions, SOPs, architectural decisions. Uses GROQ_FAST for simple questions, GROQ_MODEL for complex ones."},
    "stats":                  {"fn": t_stats,                  "perm": "READ",    "args": [],
                               "desc": "Analytics dashboard: domain distribution, top patterns, mistake frequency, evolution queue counts by status, last cold processor run. Use at session start to orient or session end to summarize."},
    "search_mistakes":        {"fn": t_search_mistakes,        "perm": "READ",    "args": ["query", "domain", "limit"],
                               "desc": "Semantic mistake search by natural language query. Use when you want mistakes related to a concept (e.g. 'railway deploy'). Use get_mistakes for domain-filtered list."},
    "changelog_add":          {"fn": t_changelog_add,          "perm": "WRITE",   "args": ["version", "component", "summary", "before", "after", "change_type"],
                               "desc": "Log a completed change to the changelog table + Telegram notify. Call after every deploy. before/after describe what changed. change_type=bugfix|feature|config|refactor."},
    "bulk_apply":             {"fn": t_bulk_apply,             "perm": "WRITE",   "args": ["executor_override", "dry_run"],
                               "desc": "Apply ALL pending evolution_queue items."},
    "repopulate":             {"fn": _repopulate_evolution_queue, "perm": "WRITE", "args": [],
                               "desc": "Re-push all P3+ backlog items to evolution_queue."},
    "list_templates":         {"fn": t_list_templates,         "perm": "READ",    "args": ["limit"],
                               "desc": "List reusable script templates."},
    "run_template":           {"fn": t_run_template,           "perm": "EXECUTE", "args": ["name", "params"],
                               "desc": "Retrieve a stored script template by name."},
    "redeploy":               {"fn": t_redeploy,               "perm": "EXECUTE", "args": ["reason"],
                               "desc": "Trigger Railway redeploy."},
    "logs":                   {"fn": t_logs,                   "perm": "READ",    "args": ["limit", "keyword"],
                               "desc": "Fetch recent Railway deployment logs."},
    "deploy_status":          {"fn": t_deploy_status,          "perm": "READ",    "args": [],
                               "desc": "Return active deploy info."},
    "build_status":           {"fn": t_build_status,           "perm": "READ",    "args": [],
                               "desc": "Check last 5 commits build state on Railway."},
    "crash_report":           {"fn": t_crash_report,           "perm": "READ",    "args": [],
                               "desc": "Detect Railway restart loops."},
    "review_evolutions":      {"fn": t_review_evolutions,      "perm": "READ",    "args": [],
                               "desc": "Get URL to the interactive evolution review widget."},
    "check_evolutions":       {"fn": t_check_evolutions,       "perm": "READ",    "args": ["limit"],
                               "desc": "Groq-powered evolution brief."},
    "search_in_file":         {"fn": t_search_in_file,         "perm": "READ",    "args": ["path", "pattern", "repo", "regex", "case_sensitive"],
                               "desc": "Search for pattern in a GitHub file."},
    "multi_patch":            {"fn": t_multi_patch,            "perm": "EXECUTE", "args": ["path", "patches", "message", "repo"],
                               "desc": "Apply multiple find-replace patches in one fetch+write."},
    "core_py_fn":             {"fn": t_core_py_fn,             "perm": "READ",    "args": ["fn_name", "file"],
                               "desc": "Read a single function from a CORE source file by name. file= param (default: core_tools.py). Pass file=core_train.py etc to read other modules."},
    "core_py_validate":       {"fn": t_core_py_validate,       "perm": "READ",    "args": [],
                               "desc": "Pre-deploy syntax checker for core_tools.py and core_main.py."},
    "system_map_scan":        {"fn": t_system_map_scan, "perm": "READ", "args": ["trigger"], "desc": "Scan system_map table. trigger=session_start|session_end|manual"},
    "session_start":          {"fn": t_session_start,          "perm": "READ",    "args": [],
                               "desc": "One-call session bootstrap. Returns: health, counts, resume_task (highest priority in_progress -- start here), in_progress_tasks, pending_tasks, recent_mistakes (last 10 all domains), stale_pattern_count, session_md (full SESSION.md static doc for claude.ai bootstrap), system_map. Use get_mistakes(domain=X) for domain-specific lookup before any write."},
    "session_end":            {"fn": t_session_end,            "perm": "WRITE",   "args": ["summary", "actions", "domain", "patterns", "quality", "skill_file_updated", "force_close", "active_task_ids"],
                               "desc": "One-call session close. actions=pipe-separated strings (| only, NOT comma). quality clamped 0.0-1.0 auto. BEFORE calling: (1) log_mistake for every error, (2) add_knowledge for every new insight, (3) changelog_add for every deploy, (4) update task statuses in task_queue. active_task_ids=pipe-separated UUIDs of tasks touched -- warns if any still pending/in_progress. TASK-21.B gate: if patterns non-empty, write rules to CORE_AGI_SKILL_V4.md then pass skill_file_updated=true. force_close=true bypasses gate. Returns: training_ok (bool), duration_seconds, reflection_warning if hot reflection failed, task_status_warnings if tasks left open."},
    "core_py_rollback":       {"fn": t_core_py_rollback,       "perm": "EXECUTE", "args": ["commit_sha"],
                               "desc": "Emergency restore: fetch any CORE file at commit_sha, write back, redeploy. file= param (default: core_main.py)."},
    "diff":                   {"fn": t_diff,                   "perm": "READ",    "args": ["path", "sha_a", "sha_b"],
                               "desc": "Compare file between two commits."},
    "deploy_and_wait":        {"fn": t_deploy_and_wait,        "perm": "EXECUTE", "args": ["reason", "timeout"],
                               "desc": "Trigger redeploy + poll until success/failure."},
    "synthesize_evolutions":  {"fn": t_synthesize_evolutions,  "perm": "READ",    "args": [],
                                 "desc": "Pure signal fetcher for Claude Desktop architect synthesis. Call when owner says 'synthesize'. Returns all accumulated signals: pending evolutions, top patterns, cold reflection themes, hot gaps, open tasks. NO Groq. NO auto task insertion. Claude reads the signals in chat, acts as architect thinking 6 months ahead, then calls task_add for each new task directly. Groq owns hot_reflection + cold_processor. Claude owns synthesis and blueprint."},
    "project_list":           {"fn": t_project_list,           "perm": "READ",    "args": [],
                               "desc": "List all registered projects: project_id, name, status, last_indexed, folder_path."},
    "project_get":            {"fn": t_project_get,            "perm": "READ",    "args": ["project_ids"],
                               "desc": "Load full context for one or more projects. project_ids=comma-separated slugs. Returns context_md ready for session injection."},
    "project_search":         {"fn": t_project_search,         "perm": "READ",    "args": ["project_id", "query"],
                               "desc": "Search KB entries for a specific project by query string."},
    "project_context_check":  {"fn": t_project_context_check,  "perm": "READ",    "args": [],
                               "desc": "Check for unconsumed prepared project contexts waiting for Claude Desktop."},
    "project_register":       {"fn": t_project_register,       "perm": "WRITE",   "args": ["project_id", "name", "folder_path", "index_path"],
                               "desc": "Register a new project in Supabase projects table."},
    "project_update_kb":      {"fn": t_project_update_kb,      "perm": "WRITE",   "args": ["project_id", "topic", "content", "confidence"],
                               "desc": "Add or update a KB entry for a project. domain=project:{project_id}."},
    "project_update_index":   {"fn": t_project_update_index,   "perm": "WRITE",   "args": ["project_id", "last_indexed"],
                               "desc": "Update last_indexed timestamp for a project in Supabase."},
    "project_prepare":        {"fn": t_project_prepare,        "perm": "WRITE",   "args": ["project_ids"],
                               "desc": "Railway-side: assemble KB context for project(s) and store in project_context for Claude Desktop to consume. Sends Telegram notify."},
    "project_consume":        {"fn": t_project_consume,        "perm": "WRITE",   "args": ["project_id"],
                               "desc": "Mark project_context rows as consumed after Claude Desktop has loaded them."},
    "ping_health":            {"fn": t_ping_health,            "perm": "READ",    "args": [],
                               "desc": "Hit live Railway / endpoint."},
    "patch_file":             {"fn": t_patch_file,            "perm": "EXECUTE", "args": ["path", "patches", "message", "repo", "dry_run"],
                               "desc": "Server-side patch: fetch from GitHub, apply find-replace patches, py_compile check, push. Safe alternative to multi_patch -- blocks deploy on syntax error."},
    "validate_syntax":        {"fn": t_validate_syntax,        "perm": "READ",    "args": ["path", "repo"],
                               "desc": "Fetch a .py file from GitHub and run py_compile server-side. Returns ok/error with line number. Use before any deploy."},
    "append_to_file":         {"fn": t_append_to_file,         "perm": "EXECUTE", "args": ["path", "content_to_append", "message", "repo"],
                               "desc": "Append content to a GitHub file server-side. Runs py_compile before push for .py files. Use to add new functions without fetching file into Claude context."},
    "verify_live":            {"fn": t_verify_live,            "perm": "READ",    "args": ["expected_text", "timeout"],
                               "desc": "Poll /state until expected_text appears."},
    "sb_patch":               {"fn": t_sb_patch,               "perm": "WRITE",   "args": ["table", "filters", "data"],
                               "desc": "Update rows in a Supabase table. filters=PostgREST filter string (e.g. id=eq.abc123) REQUIRED -- rejected if empty. data=JSON fields to update e.g. {\"status\": \"done\"}. Returns updated_fields list. Use for: updating task status, marking flags, changing any field. Never call without filters."},
    "sb_upsert":              {"fn": t_sb_upsert,              "perm": "WRITE",   "args": ["table", "data", "on_conflict"],
                               "desc": "Insert a row or update it if already exists. on_conflict=column(s) defining uniqueness (e.g. domain,topic for knowledge_base -- project_id for projects -- name for script_templates). Use instead of sb_insert when you want to avoid duplicates or update an existing entry. data=full row as JSON."},
    "sb_delete":              {"fn": t_sb_delete,              "perm": "WRITE",   "args": ["table", "filters", "confirm"],
                               "desc": "Delete rows from a Supabase table. filters=PostgREST filter string REQUIRED. confirm=DELETE (literal string) to execute -- omit for dry run showing rows that would be deleted. PROTECTED tables (sessions, mistakes, hot_reflections, cold_reflections, pattern_frequency, changelog, evolution_queue) cannot be deleted from. ALLOWED: knowledge_base, task_queue, project_context, script_templates, system_map, projects. Always dry-run first."},
}


# -- MCP JSON-RPC handler ------------------------------------------------------
def _mcp_tool_schema(name, tool):
    # Params that should be typed as array (not string) in MCP schema
    _ARRAY_PARAMS = {"patches", "project_ids", "ids", "files", "edits", "actions"}
    props = {}
    for a in tool["args"]:
        if a in _ARRAY_PARAMS:
            props[a] = {
                "type": "array",
                "description": a,
                "items": {"type": "object"}
            }
        else:
            props[a] = {"type": "string", "description": a}
    return {"name": name, "description": tool.get("desc", name),
            "inputSchema": {"type": "object", "properties": props}}


def handle_jsonrpc(body: dict, session_id: str = "") -> dict:
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")
    def ok(r):     return {"jsonrpc": "2.0", "id": req_id, "result": r}
    def err(c, m): return {"jsonrpc": "2.0", "id": req_id, "error": {"code": c, "message": m}}

    if method == "initialize":
        return ok({"protocolVersion": MCP_PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "CORE v6.0", "version": "6.0"}})
    elif method == "notifications/initialized": return None
    elif method == "ping": return ok({})
    elif method == "tools/list":
        return ok({"tools": [_mcp_tool_schema(n, t) for n, t in TOOLS.items()]})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        if tool_name not in TOOLS:
            return err(-32601, f"Unknown tool: {tool_name}")
        tool = TOOLS[tool_name]
        from core_config import L
        if not L.mcp(session_id):
            return err(-32000, "Rate limit exceeded")
        try:
            result = tool["fn"](**{k: v for k, v in tool_args.items() if k in tool["args"] or not tool["args"]})
            text = json.dumps(result, default=str)
            return ok({"content": [{"type": "text", "text": text}]})
        except Exception as e:
            return err(-32603, f"Tool error: {e}")
    return err(-32601, f"Unknown method: {method}")


# -- brain layer reconciliation ------------------------------------------------

def _reconcile_brain_tables(rows: list, inserted: list, tombstoned: list) -> None:
    """Auto-sync brain layer: diff live Supabase tables vs system_map brain entries.
    Uses management API POST /v1/projects/{ref}/database/query with SUPABASE_PAT.
    PAT is stored in knowledge_base (domain=system.config, topic=supabase_pat).
    Inserts new tables, tombstones dropped tables. Skips already-tombstoned entries.
    Called from t_system_map_scan(trigger='session_end') only.
    """
    try:
        # Fetch PAT from KB (never hardcoded in source)
        pat_rows = sb_get(
            "knowledge_base",
            "select=content&domain=eq.system.config&topic=eq.supabase_pat&limit=1",
            svc=True
        )
        if not pat_rows or not pat_rows[0].get("content"):
            print("[SMAP] brain reconcile skipped: supabase_pat not found in KB")
            return
        pat = pat_rows[0]["content"].strip()
        ref = SUPABASE_REF

        # Query live tables from management API
        mgmt_resp = httpx.post(
            f"https://api.supabase.com/v1/projects/{ref}/database/query",
            headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json"},
            json={"query": "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"},
            timeout=15
        )
        mgmt_resp.raise_for_status()
        live_tables = {row["table_name"] for row in mgmt_resp.json()}

        # Get registered brain table entries (active only)
        registered_brain = {
            row["name"]: row
            for row in rows
            if row.get("layer") == "brain"
            and row.get("item_type") == "table"
            and row.get("status") != "tombstone"
        }
        registered_names = set(registered_brain.keys())

        # Insert tables present in DB but missing from system_map
        missing = live_tables - registered_names
        for tname in sorted(missing):
            try:
                sb_post_critical("system_map", {
                    "layer": "brain",
                    "component": "supabase",
                    "item_type": "table",
                    "name": tname,
                    "role": f"Supabase table: {tname}",
                    "responsibility": "auto-registered by brain reconciliation",
                    "status": "active",
                    "updated_by": "session_end_auto",
                    "last_updated": datetime.utcnow().isoformat(),
                })
                inserted.append(f"brain:{tname}")
            except Exception as _ie:
                print(f"[SMAP] brain insert {tname} failed: {_ie}")

        # Tombstone tables in system_map that no longer exist in DB
        removed = registered_names - live_tables
        for tname in sorted(removed):
            try:
                row_id = registered_brain[tname]["id"]
                sb_patch("system_map", f"id=eq.{row_id}", {
                    "status": "tombstone",
                    "notes": "auto-tombstoned by brain reconciliation: table not found in DB",
                    "last_updated": datetime.utcnow().isoformat(),
                    "updated_by": "session_end_auto",
                })
                tombstoned.append(f"brain:{tname}")
            except Exception as _te:
                print(f"[SMAP] brain tombstone {tname} failed: {_te}")

    except Exception as e:
        print(f"[SMAP] _reconcile_brain_tables error: {e}")


# -- executor source file reconciliation ---------------------------------------

def _reconcile_executor_files(rows: list, inserted: list, tombstoned: list) -> None:
    """Auto-sync executor layer: diff live .py files in GitHub repo root vs
    system_map executor file entries. Inserts new files, tombstones removed ones.
    Uses GitHub API list repo contents -- no GITHUB_PAT scope issues.
    Called from t_system_map_scan(trigger='session_end') only.
    """
    try:
        h = _ghh()
        r = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/",
            headers=h, timeout=10
        )
        r.raise_for_status()
        live_py = {
            item["name"] for item in r.json()
            if item.get("type") == "file" and item["name"].endswith(".py")
        }

        registered_files = {
            row["name"]: row
            for row in rows
            if row.get("layer") == "executor"
            and row.get("item_type") == "file"
            and row.get("status") != "tombstone"
        }
        registered_names = set(registered_files.keys())

        # Insert .py files present in repo but missing from system_map
        missing = live_py - registered_names
        for fname in sorted(missing):
            try:
                sb_post_critical("system_map", {
                    "layer": "executor",
                    "component": "railway",
                    "item_type": "file",
                    "name": fname,
                    "role": f"Source file: {fname}",
                    "responsibility": "auto-registered by executor file reconciliation",
                    "status": "active",
                    "updated_by": "session_end_auto",
                    "last_updated": datetime.utcnow().isoformat(),
                })
                inserted.append(f"executor:{fname}")
            except Exception as _ie:
                print(f"[SMAP] executor insert {fname} failed: {_ie}")

        # Tombstone files in system_map that no longer exist in repo
        removed = registered_names - live_py
        for fname in sorted(removed):
            try:
                row_id = registered_files[fname]["id"]
                sb_patch("system_map", f"id=eq.{row_id}", {
                    "status": "tombstone",
                    "notes": "auto-tombstoned by executor reconciliation: file not found in repo root",
                    "last_updated": datetime.utcnow().isoformat(),
                    "updated_by": "session_end_auto",
                })
                tombstoned.append(f"executor:{fname}")
            except Exception as _te:
                print(f"[SMAP] executor tombstone {fname} failed: {_te}")

    except Exception as e:
        print(f"[SMAP] _reconcile_executor_files error: {e}")


# -- skeleton doc reconciliation -----------------------------------------------

def _reconcile_skeleton_docs(rows: list, inserted: list, tombstoned: list) -> None:
    """Auto-sync skeleton layer: diff live .md and .json files in GitHub repo root
    vs system_map skeleton file entries. Inserts new docs, tombstones removed ones.
    Called from t_system_map_scan(trigger='session_end') only.
    """
    try:
        h = _ghh()
        r = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/",
            headers=h, timeout=10
        )
        r.raise_for_status()
        live_docs = {
            item["name"] for item in r.json()
            if item.get("type") == "file"
            and (item["name"].endswith(".md") or item["name"].endswith(".json")
                 or item["name"].endswith(".txt"))
        }

        registered_docs = {
            row["name"]: row
            for row in rows
            if row.get("layer") == "skeleton"
            and row.get("item_type") == "file"
            and row.get("status") != "tombstone"
        }
        registered_names = set(registered_docs.keys())

        # Insert docs present in repo but missing from system_map
        missing = live_docs - registered_names
        for dname in sorted(missing):
            try:
                sb_post_critical("system_map", {
                    "layer": "skeleton",
                    "component": "github",
                    "item_type": "file",
                    "name": dname,
                    "role": f"Repository file: {dname}",
                    "responsibility": "auto-registered by skeleton doc reconciliation",
                    "status": "active",
                    "updated_by": "session_end_auto",
                    "last_updated": datetime.utcnow().isoformat(),
                })
                inserted.append(f"skeleton:{dname}")
            except Exception as _ie:
                print(f"[SMAP] skeleton insert {dname} failed: {_ie}")

        # Tombstone docs in system_map that no longer exist in repo
        removed = registered_names - live_docs
        for dname in sorted(removed):
            try:
                row_id = registered_docs[dname]["id"]
                sb_patch("system_map", f"id=eq.{row_id}", {
                    "status": "tombstone",
                    "notes": "auto-tombstoned by skeleton reconciliation: file not found in repo root",
                    "last_updated": datetime.utcnow().isoformat(),
                    "updated_by": "session_end_auto",
                })
                tombstoned.append(f"skeleton:{dname}")
            except Exception as _te:
                print(f"[SMAP] skeleton tombstone {dname} failed: {_te}")

    except Exception as e:
        print(f"[SMAP] _reconcile_skeleton_docs error: {e}")
