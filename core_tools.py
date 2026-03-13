"""core_tools.py — CORE AGI MCP tool implementations
All 50 t_* functions, TOOLS registry, _mcp_tool_schema, handle_jsonrpc.
Extracted from core.py as part of Task 2 architecture split.

Import chain:
  core_tools imports: core_config, core_github, core_train
  core_main imports: core_tools (TOOLS, handle_jsonrpc)

NOTE: This file is currently NOT active. core.py remains the live entry point.
Activation happens when core_main.py is written and smoke test passes.
"""
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
    GUARD: blocked for core.py — use gh_search_replace or multi_patch for surgical edits."""
    if (repo or GITHUB_REPO) == GITHUB_REPO and path.strip().lstrip("/") == "core.py":
        return {
            "ok": False,
            "error": "BLOCKED: write_file cannot overwrite core.py (full overwrite = corruption risk). "
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


def t_bulk_reject_evolutions(change_type: str = "", ids: str = "", reason: str = "") -> dict:
    """Bulk reject pending evolutions silently — one Telegram summary at end.
    change_type: 'backlog' | 'knowledge' | '' (all pending).
    ids: comma-separated evolution IDs (overrides change_type).
    reason: optional rejection reason."""
    id_list = [int(i.strip()) for i in ids.split(",") if i.strip().isdigit()] if ids else []
    return bulk_reject_evolutions(change_type=change_type, ids=id_list or None, reason=reason)


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
    """Surgical find-replace using Git Blobs API — no file size limit."""
    try:
        repo = repo or GITHUB_REPO
        file_content = _gh_blob_read(path, repo)
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
        commit_sha = _gh_blob_write(path, new_content, message, repo)
        return {"ok": True, "dry_run": False, "path": path,
                "replaced": old_str[:80], "commit": commit_sha[:12]}
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

def t_core_py_fn(fn_name: str) -> dict:
    """Read a single function from core.py by name."""
    try:
        content = _gh_blob_read("core.py")
        lines = content.splitlines()
        start = None
        indent = None
        for i, line in enumerate(lines):
            if line.strip().startswith(f"def {fn_name}(") or line.strip() == f"def {fn_name}()":
                start = i
                indent = len(line) - len(line.lstrip())
                break
        if start is None:
            return {"ok": False, "error": f"Function '{fn_name}' not found in core.py"}
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
    """Pre-deploy syntax checker for core.py."""
    try:
        content = _gh_blob_read("core.py")
        lines = content.splitlines()
        errors = []
        warnings = []
        size_kb = round(len(content.encode()) / 1024, 1)
        line_count = len(lines)
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("def def ") or stripped.startswith("import import "):
                errors.append(f"L{i}: double keyword — {stripped[:60]}")
        tools_close = [i+1 for i, l in enumerate(lines) if l.strip() == "}" and not lines[i].startswith(" ")]
        if len(tools_close) != 1:
            errors.append(f"TOOLS closing brace count={len(tools_close)} (expected 1) at lines {tools_close}")
        tool_fn_refs = _re.findall(r'"fn":\s*(t_\w+)', content)
        defined_fns  = set(_re.findall(r'^def (t_\w+)\(', content, _re.MULTILINE))
        for ref in tool_fn_refs:
            if ref not in defined_fns:
                errors.append(f"TOOLS refs '{ref}' but function not defined")
        for i, line in enumerate(lines, 1):
            if "backboard.railway" in line:
                errors.append(f"L{i}: stale backboard.railway reference")
        if "TOOLS = {" not in content:
            errors.append("TOOLS dict not found — critical corruption")
        if size_kb > 150:
            warnings.append(f"core.py is {size_kb}KB — consider splitting (>150KB)")
        triple_count = content.count('"""')
        if triple_count % 2 != 0:
            warnings.append(f"Odd number of triple-quotes ({triple_count}) — possible unclosed docstring")
        ok = len(errors) == 0
        return {"ok": ok, "errors": errors, "warnings": warnings,
                "line_count": line_count, "size_kb": size_kb}
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
    """Apply multiple find-replace patches via Git Blobs API."""
    try:
        repo = repo or GITHUB_REPO
        if isinstance(patches, str):
            patches = json.loads(patches)
        content = _gh_blob_read(path, repo)
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
        commit_sha = _gh_blob_write(path, content, message, repo)
        return {"ok": True, "path": path, "applied": len(applied), "skipped": len(skipped),
                "details": applied, "skipped_details": skipped, "commit": commit_sha[:12]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_session_end(summary: str, actions: str, domain: str = "general",
                  patterns: str = "", quality: str = "0.8") -> dict:
    """One-call session close. Always logs hot_reflection via auto_hot_reflection
    (Groq pattern extraction). Caller-supplied patterns are merged in."""
    from core_train import auto_hot_reflection
    try:
        actions_list = [a.strip() for a in actions.split(",") if a.strip()]
        try:
            q = float(quality)
        except:
            q = 0.8
        session_ok = sb_post("sessions", {
            "summary": summary,
            "actions": actions_list,
            "interface": "claude-desktop"
        })
        # Always run Groq-powered reflection — passes caller patterns as seed
        caller_patterns = [p.strip() for p in patterns.split("|") if p.strip()]
        r_ok = auto_hot_reflection({
            "summary": summary,
            "actions": actions_list,
            "interface": "claude-desktop",
            "domain": domain,
            "quality": q,
            "seed_patterns": caller_patterns,
        })
        reflection_id = "logged" if r_ok else "failed"
        return {
            "ok": session_ok,
            "session_logged": session_ok,
            "reflection_logged": reflection_id,
            "actions_count": len(actions_list),
            "tip": "Update SESSION.md manually if step status changed this session"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_core_py_rollback(commit_sha: str) -> dict:
    """Emergency restore: fetch core.py at any commit SHA, write back, redeploy."""
    try:
        if not commit_sha or len(commit_sha) < 6:
            return {"ok": False, "error": "commit_sha required (min 6 chars)"}
        h = _ghh()
        ref_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{commit_sha}",
                          headers=h, timeout=10)
        ref_r.raise_for_status()
        full_sha = ref_r.json()["sha"]
        short_sha = full_sha[:12]
        file_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/core.py?ref={full_sha}",
                           headers=h, timeout=30)
        file_r.raise_for_status()
        old_content = base64.b64decode(file_r.json()["content"]).decode()
        new_commit = _gh_blob_write(
            "core.py", old_content,
            f"rollback: restore core.py from {short_sha}"
        )
        deploy = t_redeploy(f"rollback to {short_sha}")
        notify_owner(f"ROLLBACK triggered — core.py restored from {short_sha}. Deploying...")
        return {
            "ok": True,
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
    """Trigger redeploy + poll until success/failure."""
    try:
        t_secs = int(timeout) if timeout else 120
        deploy_result = t_redeploy(reason)
        if not deploy_result.get("ok"):
            return {"ok": False, "error": f"redeploy failed: {deploy_result.get('error')}"}
        commit_sha_short = deploy_result.get("commit", "")
        h = _ghh()
        ref = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/git/ref/heads/main", headers=h, timeout=10)
        ref.raise_for_status()
        full_sha = ref.json()["object"]["sha"]
        deadline = time.time() + t_secs
        poll_count = 0
        while time.time() < deadline:
            time.sleep(8)
            poll_count += 1
            sr = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{full_sha}/statuses",
                           headers=h, timeout=10)
            statuses = sr.json() if sr.status_code == 200 else []
            railway = [s for s in statuses if "railway" in s.get("context","").lower()
                       or "railway" in s.get("description","").lower()]
            st = railway[0] if railway else {}
            state = st.get("state", "")
            if state == "success":
                elapsed = round(time.time() - (deadline - t_secs))
                notify_owner(f"Deploy SUCCESS — {commit_sha_short} live in {elapsed}s")
                return {"ok": True, "state": "success", "commit": commit_sha_short,
                        "description": st.get("description",""), "polls": poll_count,
                        "elapsed_s": elapsed}
            if state == "failure":
                notify_owner(f"Deploy FAILED — {commit_sha_short}")
                return {"ok": False, "state": "failure", "commit": commit_sha_short,
                        "description": st.get("description",""), "polls": poll_count}
        notify_owner(f"Deploy TIMEOUT — {commit_sha_short} after {t_secs}s")
        return {"ok": False, "state": "timeout", "commit": commit_sha_short,
                "polls": poll_count, "timeout_s": t_secs}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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


def t_route(task: str, execute: bool = False):
    """DEPRECATED: Use t_ask() instead."""
    if not task: return {"ok": False, "error": "task required"}
    sig = extract_signals(task)
    complexity = 3
    if sig["expertise"] <= 2:  complexity += 1
    if sig["emotion"] in ("urgent", "frustrated"): complexity += 1
    if sig["stakes"] == "critical": complexity += 2
    if sig["stakes"] == "high":     complexity += 1
    if sig["expertise"] >= 5:  complexity -= 1
    if sig["stakes"] == "low": complexity -= 1
    complexity = max(1, min(12, complexity))
    tone_map = {
        "urgent":     "Be concise and direct. Lead with the answer immediately.",
        "frustrated": "Acknowledge the difficulty briefly, then provide the fix directly.",
        "vulnerable": "Be warm and supportive. Slow down. Acknowledge before solving.",
        "casual":     "Match casual energy. Keep it natural and brief.",
        "neutral":    "Be clear and structured.",
    }
    expertise_map = {
        1: "Explain everything simply. Use analogies. Avoid jargon.",
        2: "Define non-obvious terms. Provide step-by-step guidance.",
        3: "Assume basic familiarity. Provide context where needed.",
        4: "Skip basics. Use domain vocabulary. Be precise.",
        5: "Expert-to-expert. Dense, precise, no hand-holding.",
    }
    disclaimer = ""
    if sig["domain"] in ("legal","medical","finance") and sig["expertise"] <= 2:
        disclaimer = "Add a brief note to verify with a professional for consequential decisions."
    system_prompt = (
        f"You are CORE, a personal AGI. "
        f"{tone_map.get(sig['emotion'], tone_map['neutral'])} "
        f"{expertise_map.get(sig['expertise'], expertise_map[3])} "
        f"Domain context: {sig['domain']}. Stakes level: {sig['stakes']}. {disclaimer} "
        "Be genuinely helpful."
    )
    routing_info = {"signals": sig, "complexity": complexity,
                    "system_prompt_preview": system_prompt[:120] + "...", "archetype": sig["archetype"]}
    if not execute:
        return {"ok": True, "routing": routing_info}
    try:
        model = GROQ_FAST if complexity <= 4 else GROQ_MODEL
        response = groq_chat(system_prompt, task, model=model)
        sb_post("task_queue", {"task": task[:300], "status": "completed", "priority": 5, "error": None, "chat_id": ""})
        return {"ok": True, "routing": routing_info, "response": response, "model_used": model}
    except Exception as e:
        return {"ok": False, "routing": routing_info, "error": str(e)}


def t_ask(question: str, domain: str = ""):
    if not question: return {"ok": False, "error": "question required"}
    kb_results = t_search_kb(question, domain=domain, limit=5)
    kb_context = "\n\n".join([f"[KB: {r.get('topic','')}]\n{str(r.get('content',''))[:300]}" for r in kb_results]) if kb_results else ""
    mistakes = t_get_mistakes(domain=domain or "general", limit=3)
    mistake_context = "\n".join([f"- Avoid: {m.get('what_failed','')} -> {m.get('correct_approach','')[:100]}" for m in mistakes]) if mistakes else ""
    system = ("You are CORE, a personal AGI assistant with accumulated knowledge from many sessions. "
              "Answer using the knowledge base context provided. Be specific and actionable.")
    user = f"Question: {question}\n\n"
    if kb_context: user += f"Relevant knowledge:\n{kb_context}\n\n"
    if mistake_context: user += f"Known pitfalls to avoid:\n{mistake_context}\n\n"
    user += "Answer:"
    try:
        answer = groq_chat(system, user, model=GROQ_FAST, max_tokens=512)
        return {"ok": True, "answer": answer, "kb_hits": len(kb_results), "question": question}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_reflect(task_summary: str, domain: str = "general", patterns: list = None,
              quality: float = None, notes: str = ""):
    ok = sb_post("hot_reflections", {
        "task_summary": task_summary[:300], "domain": domain,
        "verify_rate": 0.0, "mistake_consult_rate": 0.0,
        "new_patterns": patterns or [], "new_mistakes": [],
        "quality_score": quality, "gaps_identified": None,
        "reflection_text": notes or f"Logged via t_reflect. Domain: {domain}.",
        "processed_by_cold": False,
    })
    return {"ok": ok, "domain": domain, "patterns_count": len(patterns or [])}


def t_stats():
    try:
        hots = sb_get("hot_reflections", "select=domain,quality_score&limit=200", svc=True)
        domain_counts: Counter = Counter(h.get("domain","general") for h in hots)
        patterns = sb_get("pattern_frequency", "select=pattern_key,frequency,domain&order=frequency.desc&limit=10", svc=True)
        mistakes = sb_get("mistakes", "select=domain&limit=200", svc=True)
        mistake_counts: Counter = Counter(m.get("domain","general") for m in mistakes)
        scores = [min(1.0, max(0.0, float(h["quality_score"]))) for h in hots if h.get("quality_score") is not None]
        avg_quality = round(sum(scores) / len(scores), 2) if scores else None
        counts = get_system_counts()
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
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_search_mistakes(query: str = "", domain: str = "", limit: int = 10):
    try:
        lim = int(limit) if limit else 10
        qs = f"select=domain,context,what_failed,correct_approach,root_cause,severity&order=created_at.desc&limit={lim}"
        if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
        if query:
            word = query.split()[0]
            qs += f"&what_failed=ilike.*{word}*"
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
        # NOTE: Backlog items are NEVER pushed to evolution_queue.
        # evolution_queue is reserved for real system changes (code patches, KB writes)
        # generated by the cold processor from repeated patterns.
        # Backlog is managed exclusively in the backlog table.
    return new_items


def _sync_backlog_status():
    """No-op: backlog status is managed directly in the backlog table.
    evolution_queue coupling has been removed — backlog items are never
    pushed to evolution_queue, so there is nothing to sync from."""
    return 0


def _repopulate_evolution_queue():
    """DISABLED: Backlog items are never pushed to evolution_queue.
    evolution_queue is reserved for real system changes only.
    Backlog is managed exclusively in the backlog table.
    This function is kept as a no-op to avoid breaking any callers."""
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
    try:
        mb = int(max_batches) if max_batches else 50
        f = str(force).lower() in ("true", "1", "yes")
    except Exception:
        mb = 50; f = False
    return run_kb_mining(max_batches=mb, force=f)


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


def t_logs(limit: str = "50", keyword: str = "") -> dict:
    """Fetch recent deploy log from GitHub commit history."""
    try:
        lim = min(int(limit) if limit else 50, 50)
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
    try:
        lim = int(limit) if limit else 20
        min_p = int(min_priority) if min_priority else 1
        qs = f"select=*&status=eq.{status}&order=priority.desc&limit={lim}"
        if min_p > 1:
            qs += f"&priority=gte.{min_p}"
        if type and type.strip():
            qs += f"&type=eq.{type.strip()}"
        items = sb_get("backlog", qs, svc=True)
        total = int(httpx.get(
            f"{SUPABASE_URL}/rest/v1/backlog?select=id&limit=1",
            headers=_sbh_count_svc(), timeout=10
        ).headers.get("content-range", "*/0").split("/")[-1])
        return {"ok": True, "total": total, "filtered": len(items),
                "type_filter": type or "all", "items": items}
    except Exception as e:
        return {"ok": False, "error": str(e), "items": []}


def t_backlog_update(title: str, status: str):
    ok = sb_patch("backlog", f"title=eq.{title}", {"status": status})
    if ok:
        sb_patch("evolution_queue",
                 f"pattern_key=like.backlog%3A%25{title[:40]}%25",
                 {"status": "applied" if status == "done" else status})
    return {"ok": ok, "title": title, "new_status": status}


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
                    sb_patch("backlog", f"title=eq.{title}", {"status": "done"})
                results.append({"id": eid, "title": title, "ok": ok, "note": note})
            else:
                r = apply_evolution(eid)
                results.append({"id": eid, "title": title, "ok": r.get("ok"), "note": r.get("note", "")})
        applied = [r for r in results if r.get("ok")]
        failed  = [r for r in results if not r.get("ok") and not r.get("action")]
        notify(f"Bulk apply done\nApplied: {len(applied)} | Failed: {len(failed)} | Total: {len(results)}\nExecutor: {executor_override}")
        try:
            gh_write("BACKLOG.md", _backlog_to_markdown(),
                     f"chore(backlog): sync status after bulk_apply ({len(applied)} applied)")
        except Exception as _be:
            print(f"[BACKLOG] bulk refresh error: {_be}")
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
    try:
        h = _ghh()
        now = datetime.utcnow()
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page=5", headers=h, timeout=10)
        r.raise_for_status()
        commits = r.json()
        deploys = []
        for commit in commits:
            sha = commit["sha"]
            msg = commit.get("commit", {}).get("message", "")[:60]
            sr = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}/statuses", headers=h, timeout=10)
            statuses = sr.json() if sr.status_code == 200 else []
            railway = [s for s in statuses if "railway" in s.get("context","").lower() or "railway" in s.get("description","").lower()]
            st = railway[0] if railway else (statuses[0] if statuses else {})
            updated = st.get("updated_at", "")
            time_since = ""
            if updated:
                try:
                    dt = datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")
                    delta = now - dt
                    mins = int(delta.total_seconds() // 60)
                    time_since = f"{mins}m ago" if mins < 60 else f"{mins//60}h{mins%60}m ago"
                except: pass
            deploys.append({"commit_sha": sha[:12], "commit_msg": msg,
                            "state": st.get("state", "no status"),
                            "description": st.get("description", ""),
                            "updated_at": updated, "time_since": time_since})
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
        r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page=10", headers=h, timeout=10)
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


# -- Tool registry ------------------------------------------------------------
TOOLS = {
    "get_state":              {"fn": t_state,                  "perm": "READ",    "args": [],
                               "desc": "Get current CORE state: last session, counts, pending tasks, operating_context, session_md"},
    "get_system_health":      {"fn": t_health,                 "perm": "READ",    "args": [],
                               "desc": "Check health of all components: Supabase, Groq, Telegram, GitHub"},
    "get_constitution":       {"fn": t_constitution,           "perm": "READ",    "args": [],
                               "desc": "Get CORE immutable constitution"},
    "get_training_status":    {"fn": t_training_status,        "perm": "READ",    "args": [],
                               "desc": "Get training pipeline status: unprocessed hot, pending evolutions, thresholds"},
    "search_kb":              {"fn": t_search_kb,              "perm": "READ",    "args": ["query", "domain", "limit"],
                               "desc": "Search knowledge base"},
    "get_mistakes":           {"fn": t_get_mistakes,           "perm": "READ",    "args": ["domain", "limit"],
                               "desc": "Get recorded mistakes."},
    "read_file":              {"fn": t_read_file,              "perm": "READ",    "args": ["path", "repo"],
                               "desc": "Read file from GitHub repo."},
    "sb_query":               {"fn": t_sb_query,               "perm": "READ",    "args": ["table", "filters", "limit"],
                               "desc": "Query Supabase table."},
    "list_evolutions":        {"fn": t_list_evolutions,        "perm": "READ",    "args": ["status"],
                               "desc": "List evolutions."},
    "update_state":           {"fn": t_update_state,           "perm": "WRITE",   "args": ["key", "value", "reason"],
                               "desc": "Write state update to sessions table"},
    "add_knowledge":          {"fn": t_add_knowledge,          "perm": "WRITE",   "args": ["domain", "topic", "content", "tags", "confidence"],
                               "desc": "Add entry to knowledge base."},
    "log_mistake":            {"fn": t_log_mistake,            "perm": "WRITE",   "args": ["context", "what_failed", "fix", "domain", "root_cause", "how_to_avoid", "severity"],
                               "desc": "Log a mistake."},
    "notify_owner":           {"fn": t_notify,                 "perm": "WRITE",   "args": ["message", "level"],
                               "desc": "Send Telegram notification."},
    "sb_insert":              {"fn": t_sb_insert,              "perm": "WRITE",   "args": ["table", "data"],
                               "desc": "Insert row into Supabase table."},
    "sb_bulk_insert":         {"fn": t_sb_bulk_insert,         "perm": "WRITE",   "args": ["table", "rows"],
                               "desc": "Insert multiple rows into Supabase in one HTTP call."},
    "trigger_cold_processor": {"fn": t_trigger_cold_processor, "perm": "WRITE",   "args": [],
                               "desc": "Manually trigger cold processor."},
    "approve_evolution":      {"fn": t_approve_evolution,      "perm": "WRITE",   "args": ["evolution_id"],
                               "desc": "Approve and apply a pending evolution by ID"},
    "reject_evolution":       {"fn": t_reject_evolution,       "perm": "WRITE",   "args": ["evolution_id", "reason"],
                               "desc": "Reject a pending evolution by ID."},
    "bulk_reject_evolutions": {"fn": t_bulk_reject_evolutions, "perm": "WRITE",   "args": ["change_type", "ids", "reason"],
                               "desc": "Bulk reject pending evolutions silently. change_type=backlog|knowledge|empty, or comma-separated ids."},
    "gh_search_replace":      {"fn": t_gh_search_replace,      "perm": "EXECUTE", "args": ["path", "old_str", "new_str", "message", "repo", "dry_run"],
                               "desc": "Surgical find-and-replace in a GitHub file."},
    "gh_read_lines":          {"fn": t_gh_read_lines,          "perm": "READ",    "args": ["path", "start_line", "end_line", "repo"],
                               "desc": "Read specific line range from GitHub file."},
    "write_file":             {"fn": t_write_file,             "perm": "EXECUTE", "args": ["path", "content", "message", "repo"],
                               "desc": "Write NEW file to GitHub repo. BLOCKED for core.py."},
    "route":                  {"fn": t_route,                  "perm": "EXECUTE", "args": ["task", "execute"],
                               "desc": "DEPRECATED — use ask tool instead."},
    "ask":                    {"fn": t_ask,                    "perm": "READ",    "args": ["question", "domain"],
                               "desc": "Ask CORE anything."},
    "reflect":                {"fn": t_reflect,                "perm": "WRITE",   "args": ["task_summary", "domain", "patterns", "quality", "notes"],
                               "desc": "Log a hot reflection."},
    "stats":                  {"fn": t_stats,                  "perm": "READ",    "args": [],
                               "desc": "Analytics: domain distribution, top patterns, mistake frequency."},
    "search_mistakes":        {"fn": t_search_mistakes,        "perm": "READ",    "args": ["query", "domain", "limit"],
                               "desc": "Semantic mistake search."},
    "get_backlog":            {"fn": t_get_backlog,            "perm": "READ",    "args": ["status", "limit", "min_priority", "type"],
                               "desc": "Get improvement backlog from Supabase."},
    "backlog_update":         {"fn": t_backlog_update,         "perm": "WRITE",   "args": ["title", "status"],
                               "desc": "Update backlog item status."},
    "bulk_apply":             {"fn": t_bulk_apply,             "perm": "WRITE",   "args": ["executor_override", "dry_run"],
                               "desc": "Apply ALL pending evolution_queue items."},
    "repopulate":             {"fn": _repopulate_evolution_queue, "perm": "WRITE", "args": [],
                               "desc": "Re-push all P3+ backlog items to evolution_queue."},
    "mine_kb":                {"fn": t_mine_kb,                "perm": "WRITE",   "args": ["max_batches", "force"],
                               "desc": "Mine KB entries in batches to generate backlog items."},
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
    "core_py_fn":             {"fn": t_core_py_fn,             "perm": "READ",    "args": ["fn_name"],
                               "desc": "Read a single function from core.py by name."},
    "core_py_validate":       {"fn": t_core_py_validate,       "perm": "READ",    "args": [],
                               "desc": "Pre-deploy syntax checker for core.py."},
    "session_start":          {"fn": t_session_start,          "perm": "READ",    "args": [],
                               "desc": "One-call session bootstrap."},
    "session_end":            {"fn": t_session_end,            "perm": "WRITE",   "args": ["summary", "actions", "domain", "patterns", "quality"],
                               "desc": "One-call session close."},
    "core_py_rollback":       {"fn": t_core_py_rollback,       "perm": "EXECUTE", "args": ["commit_sha"],
                               "desc": "Emergency restore: fetch core.py at commit_sha, write back, redeploy."},
    "diff":                   {"fn": t_diff,                   "perm": "READ",    "args": ["path", "sha_a", "sha_b"],
                               "desc": "Compare file between two commits."},
    "deploy_and_wait":        {"fn": t_deploy_and_wait,        "perm": "EXECUTE", "args": ["reason", "timeout"],
                               "desc": "Trigger redeploy + poll until success/failure."},
    "ping_health":            {"fn": t_ping_health,            "perm": "READ",    "args": [],
                               "desc": "Hit live Railway / endpoint."},
    "verify_live":            {"fn": t_verify_live,            "perm": "READ",    "args": ["expected_text", "timeout"],
                               "desc": "Poll /state until expected_text appears."},
}


# -- MCP JSON-RPC handler ------------------------------------------------------
def _mcp_tool_schema(name, tool):
    props = {a: {"type": "string", "description": a} for a in tool["args"]}
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
                   "serverInfo": {"name": "CORE v5.4", "version": "5.4"}})
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
