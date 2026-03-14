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
    KNOWLEDGE_AUTO_CONFIDENCE, MCP_PROTOCOL_VERSION, SUPABASE_URL,
    L, groq_chat, sb_get, sb_post, sb_post_critical, sb_patch, sb_upsert,
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


# -- System map diff helper ---------------------------------------------------
def _sync_system_map(live_counts: dict) -> dict:
    """Compare live Supabase counts against CORE_SYSTEM_MAP.md and patch if changed.
    Called automatically at session_end. Zero-cost if nothing changed."""
    try:
        # Read current system map
        smap = gh_read("CORE_SYSTEM_MAP.md")

        # Build new counts block
        date_str = datetime.utcnow().strftime("%Y-%m-%d")
        kb  = live_counts.get("knowledge_base", 0)
        mis = live_counts.get("mistakes", 0)
        ses = live_counts.get("sessions", 0)
        hot = live_counts.get("hot_reflections", 0)
        evo = live_counts.get("evolution_queue", 0)
        tq  = live_counts.get("task_queue", 0)

        new_counts_block = (
            f"| knowledge_base | {kb:,} entries |\n"
            f"| mistakes | {mis} entries |\n"
            f"| sessions | {ses} entries |\n"
            f"| hot_reflections | {hot} entries |\n"
            f"| evolution_queue | {evo} entries |\n"
            f"| task_queue | {tq} entries |"
        )

        # Check if counts in file match live counts (simple substring check)
        counts_changed = (
            f"| knowledge_base | {kb:,} entries |" not in smap or
            f"| mistakes | {mis} entries |" not in smap or
            f"| sessions | {ses} entries |" not in smap
        )

        if not counts_changed:
            return {"updated": False, "reason": "no diff"}

        # Rebuild the counts section — find and replace the block between markers
        lines = smap.splitlines()
        new_lines = []
        in_counts = False
        replaced = False
        for line in lines:
            if "| knowledge_base |" in line and not replaced:
                in_counts = True
                continue
            if in_counts:
                if line.startswith("| task_queue |"):
                    # Inject new block
                    for cl in new_counts_block.splitlines():
                        new_lines.append(cl)
                    in_counts = False
                    replaced = True
                    continue
                else:
                    continue
            new_lines.append(line)

        if not replaced:
            return {"updated": False, "reason": "could not locate counts block"}

        # Also update _last_updated line
        final_lines = []
        for line in new_lines:
            if line.startswith("> Last updated:"):
                final_lines.append(f"> Last updated: {date_str} | Version: CORE v6.0")
            else:
                final_lines.append(line)

        new_content = "\n".join(final_lines)
        if new_content == smap:
            return {"updated": False, "reason": "no diff after rebuild"}

        ok = gh_write("CORE_SYSTEM_MAP.md", new_content,
                      f"chore(system-map): auto-sync counts — {date_str} session close")
        return {"updated": ok, "reason": "counts changed", "kb": kb, "mistakes": mis, "sessions": ses}

    except Exception as e:
        return {"updated": False, "reason": f"error: {e}"}


# -- MCP tool implementations -------------------------------------------------
def t_state():
    session = get_latest_session()
    counts  = get_system_counts()
    pending = sb_get("task_queue", "select=id,task,status&status=eq.pending&limit=5")
    try:    operating_context = json.loads(gh_read("operating_context.json"))
    except Exception as e: operating_context = {"error": f"failed to load: {e}"}
    try:    session_md = gh_read("SESSION.md")[:2000]
    except Exception as e: session_md = f"SESSION.md unavailable: {e}"
    return {"last_session": session.get("summary", "No sessions yet."),
            "last_actions": session.get("actions", []),
            "last_session_ts": session.get("created_at", ""),
            "counts": counts, "pending_tasks": pending,
            "operating_context": operating_context, "session_md": session_md}

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
    qs = f"select=domain,topic,content,confidence&limit={limit}"
    if domain: qs += f"&domain=eq.{domain}"
    if query:  qs += f"&content=ilike.*{query.split()[0]}*"
    return sb_get("knowledge_base", qs)

def t_get_mistakes(domain="", limit=10):
    try: lim = int(limit) if limit else 10
    except: lim = 10
    qs = f"select=domain,context,what_failed,correct_approach&order=created_at.desc&limit={lim}"
    if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
    return sb_get("mistakes", qs, svc=True)

def t_update_state(key, value, reason):
    ok = sb_post("sessions", {"summary": f"[state_update] {key}: {str(value)[:200]}",
                              "actions": [f"{key}={str(value)[:100]} - {reason}"], "interface": "mcp"})
    return {"ok": ok, "key": key}

def t_add_knowledge(domain, topic, content, tags="", confidence="medium"):
    tags_list = [t.strip() for t in tags.split(",")] if tags else []
    ok = sb_post("knowledge_base", {"domain": domain, "topic": topic, "content": content,
                                    "confidence": confidence, "tags": tags_list, "source": "mcp_session"})
    return {"ok": ok, "topic": topic}

def t_log_mistake(context, what_failed, fix, domain="general", root_cause="", how_to_avoid="", severity="medium"):
    ok = sb_post("mistakes", {"domain": domain, "context": context, "what_failed": what_failed,
                              "correct_approach": fix, "root_cause": root_cause or what_failed,
                              "how_to_avoid": how_to_avoid or fix, "severity": severity, "tags": []})
    return {"ok": ok}

def t_read_file(path, repo=""):
    try: return {"ok": True, "content": gh_read(path, repo or GITHUB_REPO)[:5000]}
    except Exception as e: return {"ok": False, "error": str(e)}

def t_write_file(path, content, message, repo=""):
    """Write file to GitHub repo — FULL OVERWRITE. Use for NEW files only.
    GUARD: blocked for core_main.py — use gh_search_replace or multi_patch for surgical edits."""
    if (repo or GITHUB_REPO) == GITHUB_REPO and path.strip().lstrip("/") == "core_main.py":
        return {
            "ok": False,
            "error": "BLOCKED: write_file cannot overwrite core_main.py (full overwrite = corruption risk). "
                     "Use multi_patch or gh_search_replace for surgical edits."
        }
    ok = gh_write(path, content, message, repo or GITHUB_REPO)
    if ok: notify(f"MCP write: `{path}`")
    return {"ok": ok, "path": path}

def t_notify(message, level="info"):
    icons = {"info": "i", "warn": "!", "alert": "ALERT", "ok": "OK"}
    return {"ok": notify(f"{icons.get(level, '>')} CORE\n{message}")}

def t_sb_query(table, filters="", limit=20):
    try: lim = int(limit) if limit else 20
    except: lim = 20
    qs = f"{filters}&limit={lim}" if filters else f"limit={lim}"
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

def t_training_status():
    try:
        unprocessed = sb_get("hot_reflections", "select=id&processed_by_cold=eq.0&id=gt.1", svc=True)
        pending_evo = sb_get("evolution_queue", "select=id,change_type,change_summary,confidence&status=eq.pending&id=gt.1", svc=True)
        try:
            backlog_pending = int(httpx.get(
                f"{SUPABASE_URL}/rest/v1/backlog?select=id&status=eq.pending&limit=1",
                headers=_sbh_count_svc(), timeout=10
            ).headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            backlog_pending = -1
        return {"status": f"Training pipeline ACTIVE - {get_current_step()}",
                "unprocessed_hot": len(unprocessed), "pending_evolutions": len(pending_evo),
                "backlog_pending": backlog_pending,
                "evolutions": pending_evo[:5], "cold_threshold": COLD_HOT_THRESHOLD,
                "kb_growth_threshold": COLD_KB_GROWTH_THRESHOLD,
                "kb_mine_ratio_threshold": KB_MINE_RATIO_THRESHOLD,
                "pattern_threshold": PATTERN_EVO_THRESHOLD, "auto_apply_conf": KNOWLEDGE_AUTO_CONFIDENCE}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def t_trigger_cold_processor(): return run_cold_processor()

def t_list_evolutions(status="pending"):
    rows = sb_get("evolution_queue",
                  f"select=id,status,change_type,change_summary,confidence,pattern_key,created_at&status=eq.{status}&id=gt.1&order=created_at.desc&limit=20",
                  svc=True)
    return {"evolutions": rows, "count": len(rows)}


def t_bulk_reject_evolutions(change_type: str = "", ids: str = "", reason: str = "", include_synthesized: str = "false") -> dict:
    """Bulk reject pending evolutions silently — one Telegram summary at end."""
    id_list = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()] if ids else []
    inc_syn = str(include_synthesized).lower() in ("true", "1", "yes")
    return bulk_reject_evolutions(change_type=change_type, ids=id_list or None, reason=reason, include_synthesized=inc_syn)


def t_check_evolutions(limit: int = 20) -> dict:
    """Groq-powered evolution brief."""
    try:
        evolutions = sb_get("evolution_queue",
            f"select=id,change_type,change_summary,confidence,source,recommendation,pattern_key,created_at"
            f"&status=eq.pending&id=gt.1&order=confidence.desc&limit={limit}",
            svc=True)
        mistakes = sb_get("mistakes",
            "select=domain,context,what_failed,correct_approach,root_cause,how_to_avoid,severity"
            "&order=id.desc&limit=10",
            svc=True)
        patterns = sb_get("pattern_frequency",
            "select=pattern_key,frequency,domain,description&order=frequency.desc&limit=10",
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
        # Unicode pre-flight: warn if old_str contains non-ASCII (em-dashes etc cause silent mismatches)
        if any(ord(c) > 127 for c in old_str):
            non_ascii = [f'U+{ord(c):04X}({c})' for c in old_str if ord(c) > 127][:5]
            return {"ok": False, "error": "unicode_in_old_str",
                    "hint": f"old_str contains non-ASCII chars: {non_ascii}. Use github:get_file_contents + github:create_or_update_file instead to avoid encoding mismatches.",
                    "chars": non_ascii}
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


def t_session_start() -> dict:
    """One-call session bootstrap."""
    try:
        state = t_state()
        health = t_health()
        mistakes = t_get_mistakes(domain="", limit=5)
        try:
            evolutions = sb_get("evolution_queue",
                "select=id,change_summary,change_type,confidence&status=eq.pending&order=confidence.desc&limit=5")
            if not isinstance(evolutions, list):
                evolutions = []
        except Exception:
            evolutions = []
        training = t_training_status()
        return {
            "ok": True,
            "health": health.get("overall", "unknown"),
            "components": health.get("components", {}),
            "counts": state.get("counts", {}),
            "last_session": state.get("last_session", ""),
            "last_session_ts": state.get("last_session_ts", ""),
            "pending_tasks": state.get("pending_tasks", []),
            "step": state.get("session_md", "")[:300],
            "recent_mistakes": mistakes[:5] if isinstance(mistakes, list) else [],
            "pending_evolutions": evolutions[:5] if isinstance(evolutions, list) else [],
            "unprocessed_hot": training.get("unprocessed_hot", 0),
            "pending_evo_count": training.get("pending_evolutions", 0),
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
        # Unicode pre-flight: warn if any old_str contains non-ASCII
        for i, patch in enumerate(patches):
            old = patch.get("old_str", "")
            if any(ord(c) > 127 for c in old):
                non_ascii = [f'U+{ord(c):04X}({c})' for c in old if ord(c) > 127][:5]
                return {"ok": False, "error": "unicode_in_old_str",
                        "patch_index": i,
                        "hint": f"patch[{i}] old_str contains non-ASCII chars: {non_ascii}. Use github:get_file_contents + github:create_or_update_file instead.",
                        "chars": non_ascii}
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
        ok = gh_write(path, content, message, repo)
        if not ok:
            return {"ok": False, "error": "gh_write returned False"}
        return {"ok": True, "path": path, "applied": len(applied), "skipped": len(skipped),
                "details": applied, "skipped_details": skipped}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_session_end(summary: str, actions: str, domain: str = "general",
                  patterns: str = "", quality: str = "0.8",
                  completed_tasks: str = "", new_step: str = "") -> dict:
    """One-call session close.
    completed_tasks: pipe-separated task IDs to tick in SESSION.md e.g. '7.1|7.2|7.3'
    new_step: if set, replaces the Current Step line in SESSION.md.
    Always: logs session to Supabase, appends row to SESSION.md log table, runs Groq hot_reflection,
    and auto-syncs CORE_SYSTEM_MAP.md if any counts changed this session."""
    from core_train import auto_hot_reflection
    try:
        actions_list = [a.strip() for a in actions.split(",") if a.strip()]
        try:
            q = float(quality)
        except:
            q = 0.8

        # 1. Log session to Supabase
        session_created_at = datetime.utcnow().isoformat()
        session_ok = sb_post("sessions", {
            "summary": summary,
            "actions": actions_list,
            "interface": "claude-desktop"
        })

        # 2. Always run Groq-powered hot reflection
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

        # 3. Auto-update SESSION.md
        session_md_updated = False
        try:
            content = gh_read("SESSION.md")
            original = content

            # 3a. Tick completed_tasks checkboxes
            if completed_tasks.strip():
                for task_id in completed_tasks.split("|"):
                    task_id = task_id.strip()
                    if not task_id:
                        continue
                    content = content.replace(
                        f"- [ ] {task_id} ",
                        f"- [x] {task_id} "
                    ).replace(
                        f"- [ ] {task_id}.",
                        f"- [x] {task_id}."
                    ).replace(
                        f"- [ ] {task_id}\n",
                        f"- [x] {task_id}\n"
                    )

            # 3b. Update Current Step if provided
            if new_step.strip():
                lines = content.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("## Current Step"):
                        lines[i] = f"## Current Step: {new_step.strip()}"
                        break
                content = "\n".join(lines)

            # 3c. Append row to SESSION LOG table
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
            actions_short = ", ".join(actions_list[:3])
            if len(actions_list) > 3:
                actions_short += f" (+{len(actions_list)-3} more)"
            new_row = f"| {date_str} | {summary[:60]} | {actions_short} |"
            lines = content.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("| Date |") or line.startswith("| date |"):
                    sep_line = i + 1
                    lines.insert(sep_line + 1, new_row)
                    break
            content = "\n".join(lines)

            if content != original:
                gh_write("SESSION.md", content,
                         f"chore(session): auto-update SESSION.md — {date_str} close")
                session_md_updated = True

        except Exception as e:
            print(f"[SESSION_END] SESSION.md update failed: {e}")

        # 4. Auto-sync CORE_SYSTEM_MAP.md — compare live counts, update if changed
        system_map_result = {"updated": False, "reason": "skipped"}
        try:
            live_counts = get_system_counts()
            system_map_result = _sync_system_map(live_counts)
        except Exception as e:
            system_map_result = {"updated": False, "reason": f"error: {e}"}

        return {
            "ok": session_ok,
            "session_logged": session_ok,
            "reflection_logged": reflection_id,
            "session_md_updated": session_md_updated,
            "system_map_synced": system_map_result.get("updated", False),
            "system_map_reason": system_map_result.get("reason", ""),
            "actions_count": len(actions_list),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Stubs for Telegram commands (implement fully when backlog table confirmed) --
def t_get_backlog(status: str = "pending", limit: int = 10, min_priority: int = 1) -> dict:
    """Fetch backlog items from task_queue."""
    try:
        qs = f"select=id,type,task,priority,status,description&status=eq.{status}&priority=gte.{min_priority}&order=priority.desc&limit={limit}"
        rows = sb_get("task_queue", qs, svc=True)
        if not isinstance(rows, list):
            rows = []
        # Normalize: use 'task' as 'title' if no dedicated title column
        items = []
        for r in rows:
            items.append({
                "id": r.get("id"),
                "type": r.get("type", "task"),
                "title": r.get("task", "")[:80],
                "description": r.get("description", ""),
                "priority": r.get("priority", 1),
                "status": r.get("status", "pending"),
            })
        try:
            total_r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/task_queue?select=id&limit=1",
                headers=_sbh_count_svc(), timeout=10
            )
            total = int(total_r.headers.get("content-range", "*/0").split("/")[-1])
        except Exception:
            total = -1
        return {"ok": True, "items": items, "filtered": len(items), "total": total}
    except Exception as e:
        return {"ok": False, "items": [], "filtered": 0, "total": 0, "error": str(e)}


def t_project_list() -> dict:
    """List all projects from KB (domain=project:*)."""
    try:
        rows = sb_get("knowledge_base", "select=domain,topic,content&domain=ilike.project:*&limit=50", svc=True)
        if not isinstance(rows, list):
            rows = []
        projects = []
        for r in rows:
            domain = r.get("domain", "")
            pid = domain.replace("project:", "") if domain.startswith("project:") else domain
            projects.append({
                "project_id": pid,
                "name": r.get("topic", pid),
                "status": "active",
                "summary": r.get("content", "")[:120],
            })
        return {"ok": True, "projects": projects, "count": len(projects)}
    except Exception as e:
        return {"ok": False, "projects": [], "error": str(e)}


def t_project_prepare(project_ids: str) -> dict:
    """Prepare project context — fetch KB entries for given project IDs."""
    try:
        ids = [i.strip() for i in project_ids.split(",") if i.strip()]
        prepared = []
        context = {}
        for pid in ids:
            rows = sb_get("knowledge_base",
                f"select=topic,content,confidence&domain=eq.project:{pid}&limit=20", svc=True)
            if isinstance(rows, list) and rows:
                context[pid] = rows
                prepared.append(pid)
        return {"ok": True, "prepared": prepared, "context": context}
    except Exception as e:
        return {"ok": False, "prepared": [], "error": str(e)}


# ---------------------------------------------------------------------------
# TOOLS registry — maps MCP tool names to functions + metadata
# ---------------------------------------------------------------------------
TOOLS = {
    "state":                  {"fn": t_state,               "perm": "read",  "args": {}},
    "health":                 {"fn": t_health,              "perm": "read",  "args": {}},
    "constitution":           {"fn": t_constitution,        "perm": "read",  "args": {}},
    "search_kb":              {"fn": t_search_kb,           "perm": "read",  "args": {"query": "", "domain": "", "limit": 10}},
    "get_mistakes":           {"fn": t_get_mistakes,        "perm": "read",  "args": {"domain": "", "limit": 10}},
    "update_state":           {"fn": t_update_state,        "perm": "write", "args": {"key": "", "value": "", "reason": ""}},
    "add_knowledge":          {"fn": t_add_knowledge,       "perm": "write", "args": {"domain": "", "topic": "", "content": "", "tags": "", "confidence": "medium"}},
    "log_mistake":            {"fn": t_log_mistake,         "perm": "write", "args": {"context": "", "what_failed": "", "fix": "", "domain": "general", "root_cause": "", "how_to_avoid": "", "severity": "medium"}},
    "read_file":              {"fn": t_read_file,           "perm": "read",  "args": {"path": "", "repo": ""}},
    "write_file":             {"fn": t_write_file,          "perm": "write", "args": {"path": "", "content": "", "message": "", "repo": ""}},
    "notify":                 {"fn": t_notify,              "perm": "write", "args": {"message": "", "level": "info"}},
    "sb_query":               {"fn": t_sb_query,            "perm": "read",  "args": {"table": "", "filters": "", "limit": 20}},
    "sb_insert":              {"fn": t_sb_insert,           "perm": "write", "args": {"table": "", "data": ""}},
    "sb_bulk_insert":         {"fn": t_sb_bulk_insert,      "perm": "write", "args": {"table": "", "rows": ""}},
    "training_status":        {"fn": t_training_status,     "perm": "read",  "args": {}},
    "trigger_cold_processor": {"fn": t_trigger_cold_processor, "perm": "exec", "args": {}},
    "list_evolutions":        {"fn": t_list_evolutions,     "perm": "read",  "args": {"status": "pending"}},
    "bulk_reject_evolutions": {"fn": t_bulk_reject_evolutions, "perm": "write", "args": {"change_type": "", "ids": "", "reason": "", "include_synthesized": "false"}},
    "check_evolutions":       {"fn": t_check_evolutions,    "perm": "read",  "args": {"limit": 20}},
    "approve_evolution":      {"fn": t_approve_evolution,   "perm": "write", "args": {"evolution_id": ""}},
    "reject_evolution":       {"fn": t_reject_evolution,    "perm": "write", "args": {"evolution_id": "", "reason": ""}},
    "gh_search_replace":      {"fn": t_gh_search_replace,   "perm": "write", "args": {"path": "", "old_str": "", "new_str": "", "message": "", "repo": "", "dry_run": "false"}},
    "gh_read_lines":          {"fn": t_gh_read_lines,       "perm": "read",  "args": {"path": "", "start_line": 1, "end_line": 50, "repo": ""}},
    "core_py_fn":             {"fn": t_core_py_fn,          "perm": "read",  "args": {"fn_name": "", "file": "core_tools.py"}},
    "session_start":          {"fn": t_session_start,       "perm": "read",  "args": {}},
    "session_end":            {"fn": t_session_end,         "perm": "write", "args": {"summary": "", "actions": "", "domain": "general", "patterns": "", "quality": "0.8", "completed_tasks": "", "new_step": ""}},
    "core_py_validate":       {"fn": t_core_py_validate,    "perm": "read",  "args": {}},
    "search_in_file":         {"fn": t_search_in_file,      "perm": "read",  "args": {"path": "", "pattern": "", "repo": "", "regex": "false", "case_sensitive": "false"}},
    "multi_patch":            {"fn": t_multi_patch,         "perm": "write", "args": {"path": "", "patches": "", "message": "", "repo": ""}},
    "get_backlog":            {"fn": t_get_backlog,         "perm": "read",  "args": {"status": "pending", "limit": 10, "min_priority": 1}},
    "project_list":           {"fn": t_project_list,        "perm": "read",  "args": {}},
    "project_prepare":        {"fn": t_project_prepare,     "perm": "read",  "args": {"project_ids": ""}},
}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 dispatcher — called by core_main.py MCP routes
# ---------------------------------------------------------------------------
def handle_jsonrpc(body: dict):
    """Handle a single JSON-RPC 2.0 request. Returns response dict or None for notifications."""
    method = body.get("method", "")
    params = body.get("params", {})
    req_id = body.get("id")

    # Notifications (no id) — fire and forget
    if req_id is None and method not in ("initialize", "ping"):
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    # --- MCP lifecycle ---
    if method == "initialize":
        return ok({
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {"name": "CORE AGI", "version": "6.0"},
            "capabilities": {"tools": {"listChanged": False}},
        })

    if method == "ping":
        return ok({})

    if method == "notifications/initialized":
        return None

    # --- Tool listing ---
    if method == "tools/list":
        tools_list = []
        for name, meta in TOOLS.items():
            schema_props = {}
            required = []
            for arg_name, default in meta["args"].items():
                if isinstance(default, bool):
                    schema_props[arg_name] = {"type": "boolean"}
                elif isinstance(default, int):
                    schema_props[arg_name] = {"type": "integer"}
                elif isinstance(default, float):
                    schema_props[arg_name] = {"type": "number"}
                else:
                    schema_props[arg_name] = {"type": "string"}
                # Mark as required only if default is empty string (mandatory text args)
                if default == "" and arg_name not in ("repo", "tags", "root_cause", "how_to_avoid",
                                                       "domain", "filters", "reason", "dry_run",
                                                       "patterns", "completed_tasks", "new_step",
                                                       "regex", "case_sensitive", "include_synthesized"):
                    required.append(arg_name)
            tools_list.append({
                "name": name,
                "description": (meta["fn"].__doc__ or name).strip().split("\n")[0][:120],
                "inputSchema": {
                    "type": "object",
                    "properties": schema_props,
                    "required": required,
                },
            })
        return ok({"tools": tools_list})

    # --- Tool call ---
    if method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        if tool_name not in TOOLS:
            return err(-32601, f"Tool not found: {tool_name}")
        try:
            result = TOOLS[tool_name]["fn"](**args) if args else TOOLS[tool_name]["fn"]()
            # MCP spec: result must be {content: [{type, text}]}
            result_text = json.dumps(result, default=str)
            return ok({"content": [{"type": "text", "text": result_text}]})
        except TypeError as e:
            return err(-32602, f"Invalid params for {tool_name}: {e}")
        except Exception as e:
            return err(-32603, f"Tool error ({tool_name}): {e}")

    # --- Resources (stub — not used but some clients probe) ---
    if method in ("resources/list", "prompts/list"):
        return ok({"resources": []} if "resources" in method else {"prompts": []})

    return err(-32601, f"Method not found: {method}")
