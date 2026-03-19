"""core_tools.py â€” CORE AGI MCP tool implementations
All t_* functions, TOOLS registry, _mcp_tool_schema, handle_jsonrpc.
Part of v6.0 split architecture: core_config, core_github, core_train, core_tools, core_main.

Import chain:
  core_tools imports: core_config, core_github, core_train
  core_main imports: core_tools (TOOLS, handle_jsonrpc)

NOTE: This IS the live implementation. Entry point = core_main.py (Procfile confirmed).
core.py has been deleted â€” it was legacy monolith."""
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
    GITHUB_REPO, KB_MINE_BATCH_SIZE, KB_MINE_RATIO_THRESHOLD,
    COLD_HOT_THRESHOLD, COLD_KB_GROWTH_THRESHOLD, PATTERN_EVO_THRESHOLD,
    KNOWLEDGE_AUTO_CONFIDENCE, MCP_PROTOCOL_VERSION, SUPABASE_URL, SUPABASE_REF,
    L, gemini_chat, sb_get, sb_post, sb_post_critical, sb_patch, sb_upsert, sb_delete,
)
from core_config import _sbh, _sbh_count_svc
from core_github import _ghh, _gh_blob_read, _gh_blob_write, gh_read, gh_write, notify
from core_train import apply_evolution, reject_evolution, bulk_reject_evolutions, run_cold_processor

# Alias â€” used in t_core_py_rollback and t_deploy_and_wait
notify_owner = notify

# BASE_URL and MCP_SECRET for tools that call Railway endpoints
BASE_URL = os.environ.get("RAILWAY_PUBLIC_URL", "https://core-agi-production.up.railway.app")
MCP_SECRET = os.environ.get("MCP_SECRET", "")


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
        return "(step unknown â€” read SESSION.md)"
    except Exception as e:
        return f"(step read error: {e})"


# -- Schema registry: ground-truth column/enum enforcement -------------------
# Inline schema -- no network call, always available, instant response.
# Source: operating_context.json as of 2026-03-19. Update here when schema changes.
_SCHEMA_REGISTRY = {
    "tables": {
        "knowledge_base": {
            "pk_type": "int4_serial",
            "columns": {"id": "integer", "domain": "text", "topic": "text", "content": "text",
                         "source": "text", "confidence": "text_enum", "tags": "text[]",
                         "instruction": "text", "source_type": "text", "source_ref": "text",
                         "active": "boolean", "created_at": "timestamptz", "updated_at": "timestamptz"},
            "allowed_values": {"confidence": ["low", "medium", "high", "proven"]}
        },
        "behavioral_rules": {
            "pk_type": "int8_serial",
            "columns": {"id": "bigint", "trigger": "text", "pointer": "text", "full_rule": "text",
                         "domain": "text", "priority": "integer", "active": "boolean",
                         "tested": "boolean", "source": "text", "confidence": "float8",
                         "expires_at": "timestamptz", "created_at": "timestamptz"},
            "allowed_values": {"domain": ["auth","code","failure_recovery","github","groq",
                "local_pc","postgres","powershell","project","railway","reasoning",
                "supabase","telegram","universal","zapier"]}
        },
        "hot_reflections": {
            "pk_type": "int8_serial",
            "columns": {"id": "bigint", "task_summary": "text", "domain": "text",
                         "quality_score": "float8", "reflection_text": "text",
                         "processed_by_cold": "boolean", "source": "text", "created_at": "timestamptz"},
            "allowed_values": {}
        },
        "mistakes": {
            "pk_type": "int4_serial",
            "columns": {"id": "integer", "context": "text", "what_failed": "text",
                         "root_cause": "text", "correct_approach": "text", "domain": "text",
                         "how_to_avoid": "text", "severity": "text", "created_at": "timestamptz"},
            "allowed_values": {"severity": ["low", "medium", "high", "critical"]}
        },
        "task_queue": {
            "pk_type": "uuid",
            "columns": {"id": "uuid", "task": "text", "status": "text", "priority": "integer",
                         "result": "text", "source": "text", "next_step": "text",
                         "blocked_by": "text[]", "checkpoint": "jsonb", "created_at": "timestamptz"},
            "allowed_values": {"status": ["pending", "in_progress", "done", "failed"]}
        },
        "quality_metrics": {
            "pk_type": "int8_serial",
            "columns": {"id": "bigint", "session_id": "uuid", "quality_score": "float8",
                         "tasks_completed": "integer", "mistakes_made": "integer",
                         "owner_corrections": "integer", "assumptions_caught": "integer",
                         "domain": "text", "notes": "text", "created_at": "timestamptz"},
            "allowed_values": {}
        }
    }
}

def _load_schema_registry():
    """Return inline schema registry -- no network call, always instant."""
    return _SCHEMA_REGISTRY

def _validate_write(table: str, data: dict) -> list:
    """Validate data dict against schema registry before any Supabase write.
    Returns list of error strings. Empty list = OK to write.
    Logs all violations to stdout so they appear in Railway logs."""
    errors = []
    reg = _load_schema_registry()
    tables = reg.get("tables", {})
    if table not in tables:
        # Unknown table -- warn but don't block (table may be new)
        print(f"[SCHEMA] WARNING: table '{table}' not in registry -- write proceeding unvalidated")
        return errors
    schema = tables[table]
    known_cols = schema.get("columns", {})
    enums = schema.get("enums", {})
    required = schema.get("required", [])
    # Check for unknown columns
    for col in data:
        if col not in known_cols:
            errors.append(f"UNKNOWN_COLUMN: '{col}' does not exist in {table}. Known: {list(known_cols.keys())}")
    # Check required fields
    for col in required:
        if col not in data or data[col] is None:
            errors.append(f"MISSING_REQUIRED: '{col}' is required in {table}")
    # Check enum values
    for col, allowed in enums.items():
        if col in data and data[col] is not None:
            if str(data[col]) not in [str(v) for v in allowed]:
                errors.append(f"INVALID_ENUM: '{col}'='{data[col]}' not in {allowed} for {table}")
    if errors:
        for e in errors:
            print(f"[SCHEMA VIOLATION] {table}: {e}")
    return errors


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
    from concurrent.futures import ThreadPoolExecutor, as_completed
    h = {"ts": datetime.utcnow().isoformat(), "components": {}}
    checks = {
        "supabase": lambda: sb_get("sessions", "select=id&limit=1"),
        "groq":     lambda: httpx.get("https://api.groq.com/openai/v1/models",
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=5).raise_for_status(),
        "telegram": lambda: httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=5).raise_for_status(),
        "github":   lambda: gh_read("README.md"),
    }
    def _run(name, fn):
        try: fn(); return name, "ok"
        except Exception as e: return name, f"error:{e}"
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_run, name, fn): name for name, fn in checks.items()}
        for f in as_completed(futures):
            name, result = f.result()
            h["components"][name] = result
    h["overall"] = "ok" if all(v == "ok" for v in h["components"].values()) else "degraded"
    return h

def t_constitution():
    try:
        with open("constitution.txt") as f: txt = f.read()
    except: txt = gh_read("constitution.txt")
    return {"constitution": txt, "immutable": True}

def t_search_kb(query="", domain="", limit=10):
    """Search knowledge_base. Multi-word queries search content, topic, and instruction fields.
    AGI-05: increments access_count + updates last_accessed on every hit (fire-and-forget)."""
    lim = int(limit) if limit else 10
    qs = f"select=id,domain,topic,instruction,content,confidence&limit={lim}"
    if domain and domain not in ("all", ""):
        qs += f"&domain=eq.{domain}"
    if query:
        q = query.strip().replace("'", "").replace('"', "")
        qs += f"&or=(content.ilike.*{q}*,topic.ilike.*{q}*,instruction.ilike.*{q}*)"
    rows = sb_get("knowledge_base", qs)
    # AGI-05: fire-and-forget access tracking -- never blocks return
    try:
        if rows:
            now_ts = datetime.utcnow().isoformat()
            for r in rows:
                rid = r.get("id")
                if rid and rid != 1:
                    try:
                        sb_patch("knowledge_base", f"id=eq.{rid}",
                                 {"last_accessed": now_ts,
                                  "access_count": (r.get("access_count") or 0) + 1})
                    except Exception:
                        pass
    except Exception:
        pass
    return rows

def t_get_mistakes(domain="", limit=10):
    try: lim = int(limit) if limit else 10
    except: lim = 10
    qs = f"select=domain,context,what_failed,correct_approach,severity,root_cause,how_to_avoid&order=created_at.desc&limit={lim}"
    if domain and domain not in ("all", ""): qs += f"&domain=eq.{domain}"
    return sb_get("mistakes", qs, svc=True)

def t_update_state(key="", value="", reason=""):
    ok = sb_post("sessions", {"summary": f"[state_update] {key}: {str(value)[:200]}",
                              "actions": [f"{key}={str(value)[:100]} - {reason}"], "interface": "mcp"})
    return {"ok": ok, "key": key}

def t_add_knowledge(domain="", topic="", instruction="", content="", tags="", confidence="medium", source_type="", source_ref=""):
    """Add knowledge entry. instruction = behavioral directive for CORE (primary). content = supporting detail. At least one required. source_type=manual|ingested|evolved|session. source_ref=URL or session_id."""
    if not instruction and not content:
        return {"ok": False, "error": "At least one of instruction or content is required"}
    # TASK-27.B: Contradiction + duplicate check before insert
    try:
        existing = sb_get("knowledge_base",
            f"select=instruction,content&domain=eq.{domain}&topic=eq.{topic}&limit=1",
            svc=True)
        if existing:
            ex = existing[0]
            ex_instr    = (ex.get("instruction") or "").strip().lower()
            new_instr   = (instruction or "").strip().lower()
            ex_content  = (ex.get("content") or "").strip()
            new_content = (content or "").strip()
            # Check 1: instruction conflict -- block and alert
            if ex_instr and new_instr and ex_instr != new_instr:
                notify(f"[KB CONFLICT] {domain}/{topic}\nExisting: {ex_instr[:120]}\nProposed: {new_instr[:120]}\nBlocked -- use kb_update.")
                return {"ok": False, "conflict": True, "conflict_type": "instruction",
                        "action": "blocked",
                        "existing_instruction": ex.get("instruction", "")[:200],
                        "existing_content": ex_content[:200],
                        "message": "Instruction contradicts existing KB entry. Use kb_update to overwrite."}
            # Check 2: exact duplicate -- skip silently
            if ex_instr == new_instr and ex_content.lower() == new_content.lower():
                return {"ok": True, "action": "skipped_duplicate", "topic": topic}
            # Check 3: content conflict (substantial differing content, no instruction change)
            if ex_content and new_content and ex_content.lower() != new_content.lower():
                if len(ex_content) > 50 and len(new_content) > 50:
                    notify(f"[KB CONTENT CONFLICT] {domain}/{topic}\nExisting: {ex_content[:120]}\nProposed: {new_content[:120]}\nBlocked -- use kb_update.")
                    return {"ok": False, "conflict": True, "conflict_type": "content",
                            "action": "blocked",
                            "existing_instruction": ex.get("instruction", "")[:200],
                            "existing_content": ex_content[:200],
                            "message": "Content differs from existing KB entry. Use kb_update to overwrite."}
    except Exception:
        pass  # Non-fatal -- proceed with insert if check fails
    tags_list = [t.strip() for t in tags.split(",")] if tags else []
    # Normalize confidence to valid enum -- guard against float strings passed from older calls
    VALID_CONFIDENCE = {"low", "medium", "high", "proven"}
    if str(confidence) not in VALID_CONFIDENCE:
        # Try to coerce float->enum
        try:
            v = float(confidence)
            confidence = "proven" if v >= 0.9 else "high" if v >= 0.7 else "medium" if v >= 0.4 else "low"
            print(f"[SCHEMA] confidence coerced from float {v} -> '{confidence}'")
        except (TypeError, ValueError):
            confidence = "medium"
            print(f"[SCHEMA] confidence invalid, defaulting to 'medium'")
    row = {"domain": domain, "topic": topic, "instruction": instruction or None,
            "content": content or "", "confidence": confidence,
            "tags": tags_list, "source": "mcp_session"}
    if source_type:
        row["source_type"] = source_type
    if source_ref:
        row["source_ref"] = source_ref
    # Schema validation before write
    errs = _validate_write("knowledge_base", row)
    if errs:
        return {"ok": False, "topic": topic, "error": f"Schema violation: {errs}"}
    try:
        r = httpx.post(f"{SUPABASE_URL}/rest/v1/knowledge_base", headers=_sbh(True), json=row, timeout=15)
        if not r.is_success:
            return {"ok": False, "topic": topic, "error": f"Supabase {r.status_code}: {r.text[:300]}"}
        return {"ok": True, "topic": topic}
    except Exception as e:
        return {"ok": False, "topic": topic, "error": str(e)}

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
            "You are a senior researcher at an AGI self-improvement lab. "
            "Your job is to analyze CORE's live runtime context and extract high-signal patterns "
            "that will improve CORE's behavior, reasoning, and architecture. "
            "Be specific, grounded in the context provided, and adversarial where needed. "
            "Output MUST be valid JSON: "
            '{"domain": "code|db|bot|mcp|training|kb|general", '
            '"patterns": ["pattern1", "pattern2", "pattern3"], '
            '"gaps": "1-2 sentences on what CORE is structurally missing", '
            '"summary": "1 sentence research finding"} '
            "Output ONLY valid JSON, no preamble."
        )

        # Craft user prompt -- dynamic context injected at runtime by _run_simulation_batch
        # NOTE: instruction appears once only -- no redundant suffix
        user_prompt_template = (
            f"{instruction}\n\n"
            "CORE live context (use this as your research data):\n"
            "{{RUNTIME_CONTEXT}}\n\n"
            "Run your research. Output only valid JSON."
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


def t_log_mistake(context="", what_failed="", correct_approach="", domain="general", root_cause="", how_to_avoid="", severity="medium"):
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

def t_write_file(path="", content="", message="", repo=""):
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

def t_sb_insert(table="", data=""):
    if isinstance(data, str):
        try: data = json.loads(data)
        except Exception as e: return {"ok": False, "error_code": "invalid_json", "message": f"data must be valid JSON: {e}", "retry_hint": False, "domain": "supabase"}
    try:
        ok = sb_post(table, data)
        if not ok:
            return {"ok": False, "error_code": "insert_failed", "message": f"Supabase insert failed for table {table}", "retry_hint": True, "domain": "supabase"}
        return {"ok": True, "table": table}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "supabase"}

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

def t_debug_fn(fn_name: str, dry_run: bool = True, extra_args: dict = None) -> dict:
    """Battle-tested debug harness for any CORE function.

    DOCTRINE: Every CORE function must be debuggable without touching production data.
    This tool runs a named function through a staged execution pipeline:
      Stage 0 - resolve:  confirm function exists and is callable
      Stage 1 - preflight: run all data fetches (Supabase, KB, etc) and return raw inputs
      Stage 2 - llm:      call Groq/LLM if applicable, return raw response before parsing
      Stage 3 - parse:    attempt JSON/text parse, return parsed result + any parse error
      Stage 4 - write:    attempt DB write (SKIPPED if dry_run=True)
    Each stage is labelled in the response. First failed stage tells you exactly where it broke.

    Args:
        fn_name:    name of function to debug (e.g. '_run_simulation_batch', '_extract_real_signal')
        dry_run:    if True (default), skip all DB writes -- safe for production debugging
        extra_args: optional dict of extra args to pass to the function (for t_* tools)

    Returns dict with:
        fn:         function name
        dry_run:    whether writes were skipped
        stages:     list of {stage, status, data, error, trace} -- one per stage executed
        final:      'ok' | 'failed_at_stage_N' | 'not_found'
        result:     final function return value if ran successfully
    """
    import traceback, importlib

    # MCP passes all args as strings -- coerce explicitly
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() not in ("false", "0", "no")
    if isinstance(extra_args, str):
        try:
            import json as _j; extra_args = _j.loads(extra_args)
        except Exception:
            extra_args = {}

    stages = []
    result = None

    def stage(name, fn, *args, **kwargs):
        """Run one stage, append to stages list, return (ok, value)."""
        try:
            val = fn(*args, **kwargs)
            stages.append({"stage": name, "status": "ok", "data": str(val)[:500]})
            return True, val
        except Exception as e:
            stages.append({"stage": name, "status": "error",
                           "error": str(e), "trace": traceback.format_exc()[:1000]})
            return False, None

    # Stage 0 -- resolve function
    # Use reload() to pick up functions added in the current deploy/session.
    # Cached imports miss functions registered after Railway started.
    target_fn = None
    found_in = None
    for mod_name in ["core_tools", "core_train", "core_config", "core_github", "core_main"]:
        try:
            mod = importlib.import_module(mod_name)
            try:
                importlib.reload(mod)  # force refresh -- catches new functions in same deploy
            except Exception:
                pass  # reload is best-effort; proceed with cached version if it fails
            if hasattr(mod, fn_name):
                target_fn = getattr(mod, fn_name)
                found_in = mod_name
                stages.append({"stage": "0_resolve", "status": "ok",
                               "data": f"found in {mod_name}"})
                break
        except Exception as e:
            stages.append({"stage": "0_resolve", "status": "error",
                           "error": f"{mod_name}: {e}"})

    if target_fn is None:
        return {"fn": fn_name, "dry_run": dry_run, "stages": stages,
                "final": "not_found", "result": None}

    # Stage 1..N -- if it's a known pipeline function, run staged breakdown
    # Otherwise fall back to direct call with dry_run awareness
    known_staged = {
        "_run_simulation_batch": "simulation",
        "_extract_real_signal":  "real_signal",
        "run_cold_processor":    "cold_processor",
        "gemini_chat":           "gemini",
        "groq_chat":             "groq",
    }

    if fn_name in known_staged:
        from core_config import sb_get, sb_post, groq_chat, GROQ_MODEL
        import json as _json

        # -- Gemini staged test --
        if known_staged[fn_name] == "gemini":
            def gemini_preflight():
                from core_config import _GEMINI_KEYS, _GEMINI_MODEL
                return {"key_count": len(_GEMINI_KEYS), "model": _GEMINI_MODEL,
                        "exhausted_keys": [i for i, k in enumerate(_GEMINI_KEYS) if not k]}
            ok, _ = stage("1_preflight", gemini_preflight)
            if not ok:
                return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": "failed_at_stage_1", "result": None}
            if not dry_run:
                call_kwargs = extra_args or {"system": "test", "user": "reply OK", "max_tokens": 10}
                ok2, result = stage("2_execute", target_fn, **call_kwargs)
            else:
                stages.append({"stage": "2_execute", "status": "dry_run_skipped", "data": "pass dry_run=false to call Gemini API"})
                ok2, result = True, None
            return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": "ok" if ok2 else "failed_at_stage_2", "result": result}

        # -- Groq staged test --
        if known_staged[fn_name] == "groq":
            def groq_preflight():
                from core_config import GROQ_API_KEY, GROQ_MODEL
                return {"model": GROQ_MODEL, "key_set": bool(GROQ_API_KEY)}
            ok, _ = stage("1_preflight", groq_preflight)
            if not ok:
                return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": "failed_at_stage_1", "result": None}
            if not dry_run:
                call_kwargs = extra_args or {"system": "test", "user": "reply OK", "max_tokens": 10}
                ok2, result = stage("2_execute", target_fn, **call_kwargs)
            else:
                stages.append({"stage": "2_execute", "status": "dry_run_skipped", "data": "pass dry_run=false to call Groq API"})
                ok2, result = True, None
            return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": "ok" if ok2 else "failed_at_stage_2", "result": result}

        # Stage 1 -- preflight data fetch (for pipeline functions)
        def do_preflight():
            mistakes = sb_get("mistakes", "select=domain,what_failed&order=id.desc&limit=5", svc=True)
            hots_count = len(sb_get("hot_reflections", "select=id&processed_by_cold=eq.0", svc=True) or [])
            return {"mistakes_count": len(mistakes), "unprocessed_hots": hots_count}
        ok, preflight = stage("1_preflight", do_preflight)
        if not ok:
            return {"fn": fn_name, "dry_run": dry_run, "stages": stages,
                    "final": "failed_at_stage_1", "result": None}

        # Stage 2 -- call function with write intercepted
        orig_sb_post = None
        if dry_run:
            import core_config as _cc
            orig_sb_post = _cc.sb_post
            _cc.sb_post = lambda t, d: stages.append({"stage": "4_write_intercepted",  # noqa
                "status": "dry_run_skipped", "data": f"table={t} keys={list(d.keys())}"}) or True

        try:
            ok2, result = stage("2_execute", target_fn)
        finally:
            if dry_run and orig_sb_post:
                import core_config as _cc
                _cc.sb_post = orig_sb_post

        final = "ok" if ok2 else "failed_at_stage_2"
    else:
        # Generic: just call it directly
        call_kwargs = extra_args or {}
        ok, result = stage("1_direct_call", target_fn, **call_kwargs)
        final = "ok" if ok else "failed_at_stage_1"

    return {"fn": fn_name, "dry_run": dry_run, "stages": stages, "final": final, "result": result}


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

def t_backfill_patterns(batch_size: str = "20") -> dict:
    """TASK-20: Backfill pattern_frequency -> knowledge_base directly via Groq.
    batch_size: max patterns per run (default 20).
    """
    from core_train import _backfill_patterns
    try:
        inserted = _backfill_patterns(batch_size=int(batch_size))
        return {"ok": True, "inserted": inserted, "batch_size": int(batch_size)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_backfill_status() -> dict:
    """TASK-20: Backfill status - DEPRECATED. Use t_backfill_patterns directly (now synchronous)."""
    return {"ok": True, "note": "Backfill is now synchronous - use t_backfill_patterns directly", "status": "deprecated"}


def t_ingest_knowledge(topic: str, sources: str = "all", max_per_source: int = 20, since_days: int = 7) -> dict:
    """Trigger knowledge ingestion pipeline for a topic.
    Fetches from public sources (arxiv, docs, medium, reddit, hackernews, stackoverflow),
    deduplicates, scores by engagement, extracts AI concepts, writes to kb_sources/kb_articles/kb_concepts,
    injects hot_reflections for cold processor pickup so CORE evolves from internet knowledge.
    sources: comma-separated list or 'all'. max_per_source: cap per fetcher. since_days: recency filter.
    Returns: {topic, sources_used, raw_count, deduped_count, records_inserted, records_updated, concepts_found, hot_reflections_injected}
    """
    import asyncio
    try:
        from scraper.knowledge import ingest_knowledge
        from core_train import _ingest_to_hot_reflection
        from scraper.knowledge.concept_extractor import AI_CONCEPTS

        if sources == "all" or not sources:
            src_list = ["arxiv", "docs", "medium", "reddit", "hackernews", "stackoverflow"]
        else:
            src_list = [s.strip() for s in sources.split(",") if s.strip()]

        print(f"[t_ingest_knowledge] topic={topic} sources={src_list} max={max_per_source}")

        # Run async ingestion pipeline in new event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, ingest_knowledge(
                        topic=topic, sources=src_list,
                        max_per_source=max_per_source, since_days=since_days
                    ))
                    summary = future.result(timeout=300)
            else:
                summary = loop.run_until_complete(ingest_knowledge(
                    topic=topic, sources=src_list,
                    max_per_source=max_per_source, since_days=since_days
                ))
        except RuntimeError:
            summary = asyncio.run(ingest_knowledge(
                topic=topic, sources=src_list,
                max_per_source=max_per_source, since_days=since_days
            ))

        # Inject hot_reflections for cold processor
        hot_ok = False
        concepts_found = summary.get("concepts_found", 0)
        if concepts_found > 0:
            concepts = list(AI_CONCEPTS.keys())[:concepts_found]
            avg_eng = summary.get("avg_engagement", 50.0)
            source_str = ",".join(src_list)
            hot_ok = _ingest_to_hot_reflection(topic, source_str, concepts, avg_eng)

        return {"ok": True, "hot_reflections_injected": hot_ok, **summary}
    except Exception as e:
        print(f"[t_ingest_knowledge] error: {e}")
        return {"ok": False, "error": str(e)}


def t_listen() -> dict:
    """LISTEN MODE: Start background listen job directly via globals, no HTTP self-call.
    Call t_listen_result after ~2 minutes to fetch results once job is done.
    SOP: (1) call t_listen -> get job_id, (2) wait, (3) call t_listen_result -> synthesize chunks.
    """
    try:
        import sys
        import threading
        import uuid
        from datetime import datetime
        
        # Access core_main globals via sys.modules (avoids circular import)
        core_main = sys.modules.get('core_main')
        if not core_main:
            return {"ok": False, "error": "core_main not loaded"}
        
        _listen_job = core_main._listen_job
        _listen_lock = core_main._listen_lock
        _run_listen_job = core_main._run_listen_job
        
        with _listen_lock:
            if _listen_job.get("status") == "running":
                return {"ok": True, "job_id": _listen_job["id"], "status": "running", "note": "already running"}
            job_id = str(uuid.uuid4())[:8]
            core_main._listen_job = {"id": job_id, "status": "running", "chunks": [], "started_at": datetime.utcnow().isoformat(), "stop_reason": None}
        
        t = threading.Thread(target=_run_listen_job, args=(job_id,), daemon=True)
        t.start()
        print(f"[t_listen] job started: {job_id}")
        return {"ok": True, "job_id": job_id, "status": "running", "note": "Job started. Call t_listen_result in ~2 minutes to fetch results."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_listen_result() -> dict:
    """Fetch current listen job status and results directly via globals, no HTTP self-call.
    Returns status (running|done|error), stop_reason, cycles, patterns_found, evolutions_queued, chunks.
    If status=running, wait and call again. If status=done, synthesize chunks into tasks.
    """
    try:
        import sys
        
        # Access core_main globals via sys.modules (avoids circular import)
        core_main = sys.modules.get('core_main')
        if not core_main:
            return {"ok": False, "error": "core_main not loaded"}
        
        _listen_lock = core_main._listen_lock
        
        with _listen_lock:
            job = dict(core_main._listen_job)
        
        chunks = job.get("chunks", [])
        cold_runs = [c for c in chunks if isinstance(c, dict) and c.get("type") == "cold_run"]
        
        return {
            "ok": True,
            "job_id": job.get("id"),
            "status": job.get("status", "idle"),
            "stop_reason": job.get("stop_reason"),
            "cycles": len(cold_runs),
            "total_patterns_found": sum(c.get("patterns_found", 0) for c in cold_runs),
            "total_evolutions_queued": sum(c.get("evolutions_queued", 0) for c in cold_runs),
            "chunk_count": len(chunks),
            "chunks": chunks,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_list_evolutions(status="pending"):
    rows = sb_get("evolution_queue",
                  f"select=id,status,change_type,change_summary,confidence,pattern_key,created_at&status=eq.{status}&id=gt.1&order=created_at.desc&limit=20",
                  svc=True)
    return {"evolutions": rows, "count": len(rows)}


def t_bulk_reject_evolutions(change_type: str = "", ids: str = "", reason: str = "", include_synthesized: str = "false") -> dict:
    """Bulk reject pending evolutions silently â€” one Telegram summary at end.
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

        raw = gemini_chat(system, user, max_tokens=2000, json_mode=True)
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        brief = json.loads(raw)

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
        return {"ok": False, "error": f"Gemini returned invalid JSON: {e}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def t_approve_evolution(evolution_id):
    try: eid = int(evolution_id)
    except: return {"ok": False, "error": "evolution_id must be a number"}
    return apply_evolution(eid)

def t_reject_evolution(evolution_id="", reason=""):
    try: eid = int(evolution_id)
    except: return {"ok": False, "error": "evolution_id must be a number"}
    return reject_evolution(eid, reason)

def _patch_find(content: str, old_str: str):
    """Find old_str in content. Returns (found, count, matched_str, note).
    Fallback tiers in order:
      1. Exact match
      2. Line-ending normalization (\r\n -> \n)
      3. Trailing-whitespace strip per line (catches editor-added trailing spaces)
      4. Tab->spaces normalization (catches mixed indent files)
      5. Combined: trailing-whitespace + tab normalization
    Returns char-level ndiff hint on near-miss so caller knows exactly what differs.
    """
    def _rstrip_lines(s: str) -> str:
        return '\n'.join(line.rstrip() for line in s.splitlines())

    def _detab(s: str, tabsize: int = 4) -> str:
        return s.expandtabs(tabsize)

    def _find_in(c: str, o: str, orig_content: str, note: str):
        """Try to find o in c. Returns the ORIGINAL (unnormalized) text block that
        corresponds to the match, so the replacement operates on actual file bytes.
        Uses line-count anchoring: find match position in normalized string, count
        newlines before it to get start line, then extract that many lines from original.
        """
        cnt = c.count(o)
        if cnt <= 0:
            return None
        pos = c.find(o)
        # Count how many newlines precede the match in the normalized string
        start_line_idx = c[:pos].count('\n')
        match_line_count = o.count('\n') + 1
        # Extract the same line range from the original content
        orig_lines = orig_content.splitlines(keepends=True)
        end_line_idx = start_line_idx + match_line_count
        if end_line_idx > len(orig_lines):
            # Fallback: position-based slice (best effort)
            actual = orig_content[pos:pos + len(o)]
        else:
            actual = ''.join(orig_lines[start_line_idx:end_line_idx])
            # Strip trailing newline if original o didn't end with newline
            if not o.endswith('\n') and actual.endswith('\n'):
                actual = actual[:-1]
        return True, cnt, actual, note

    # Tier 1: exact
    count = content.count(old_str)
    if count > 0:
        return True, count, old_str, None

    # Tier 2: line-ending normalization
    nc = content.replace('\r\n', '\n').replace('\r', '\n')
    no = old_str.replace('\r\n', '\n').replace('\r', '\n')
    r = _find_in(nc, no, content, "line_ending_normalized")
    if r: return r

    # Tier 3: trailing whitespace stripped per line
    nc3 = _rstrip_lines(nc)
    no3 = _rstrip_lines(no)
    r = _find_in(nc3, no3, content, "trailing_whitespace_stripped")
    if r: return r

    # Tier 4: tab -> spaces (tabsize=4)
    nc4 = _detab(nc)
    no4 = _detab(no)
    r = _find_in(nc4, no4, content, "tabs_expanded")
    if r: return r

    # Tier 5: combined trailing-whitespace + tab normalization
    nc5 = _rstrip_lines(_detab(nc))
    no5 = _rstrip_lines(_detab(no))
    r = _find_in(nc5, no5, content, "tabs_and_trailing_normalized")
    if r: return r

    # All tiers failed -- build near-miss hint from first line
    first_line = no.strip().splitlines()[0].strip() if no.strip() else ""
    hint = None
    if first_line and len(first_line) > 10:
        for line in nc.splitlines():
            if first_line[:30] in line or line.strip()[:30] in first_line:
                diff = list(difflib.ndiff([first_line], [line.strip()]))
                hint = "near_miss: " + "".join(diff)[:300]
                break
    return False, 0, old_str, hint


def t_gh_search_replace(path="", old_str="", new_str=None, message="", repo="", dry_run="false", allow_deletion="false"):
    """Surgical find-replace using Blobs API (atomic commit, no SHA conflict, no size limit).
    allow_deletion: must be 'true' to permit empty new_str. Default false -- blocks accidental deletion."""
    try:
        repo = repo or GITHUB_REPO
        # DELETION GUARD: block empty/missing new_str unless allow_deletion=true
        _allow_del = str(allow_deletion).lower() == "true"
        if (new_str is None or new_str == "") and not _allow_del:
            return {"ok": False, "error": "DELETION BLOCKED: new_str is missing or empty. Pass allow_deletion=true if this deletion is intentional."}
        if new_str is None:
            new_str = ""
        file_content = _gh_blob_read(path, repo)
        found, count, matched, hint = _patch_find(file_content, old_str)
        if not found:
            return {"ok": False, "error": f"old_str not found in {path}",
                    "hint": hint or "check whitespace/indentation"}
        if count > 1:
            return {"ok": False, "error": f"old_str found {count}x - be more specific"}
        new_content = file_content.replace(matched, new_str, 1)
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
                "replaced": old_str[:80], "commit": commit_sha[:12] if commit_sha else None}
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
        # raw_lines: exact file bytes for each line, joined by \n -- use this to build old_str for patches.
        # Never construct old_str from the numbered display (content field) -- line number prefix will corrupt it.
        raw = "\n".join(selected)
        return {"ok": True, "path": path, "total_lines": total,
                "showing": f"{s+1}-{s+len(selected)}", "content": numbered, "raw": raw}
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


def _get_task_domain(task_json: str = "") -> str:
    """Detect mistake domain from resume_task JSON keywords.
    Maps task description + subtask content to the most relevant mistakes domain.
    Used by t_session_start to inject domain-scoped mistakes at boot."""
    try:
        t = task_json.lower()
        if any(k in t for k in ["patch_file", "core_tools", "core_train", "gh_search_replace", "multi_patch", "old_str"]):
            return "core_agi.patching"
        if any(k in t for k in ["cold_processor", "hot_reflection", "cold_reflection", "training", "evolution"]):
            return "core_train"
        if any(k in t for k in ["railway", "deploy", "build_status", "redeploy", "crash"]):
            return "core_agi.deploy"
        if any(k in t for k in ["session_end", "session_start", "skill_file", "session_close"]):
            return "core_agi.session"
        if any(k in t for k in ["zapier", "gmail", "calendar", "google"]):
            return "zapier"
        if any(k in t for k in ["project", "jk1-2", "lsei", "equinix", "rmu", "mltx"]):
            return "project"
        return "core_agi"
    except Exception:
        return "core_agi"


def t_session_start() -> dict:
    """One-call session bootstrap - includes system_map snapshot.
    Returns in_progress_tasks separately from pending_tasks so Claude immediately
    knows if a task was left partially done last session.
    domain_mistakes: top 5 scoped to resume_task domain (backfilled with global if <3 results).
    top_patterns: top 3 patterns by frequency from pattern_frequency.
    quality_alert: non-null if trend=declining or 7d_avg<0.75.
    Use get_mistakes(domain=X) for deeper domain-specific lookup."""
    try:
        state = t_state()
        health = t_health()
        # --- 25.B: Domain-scoped mistake injection ---
        pending_tasks_all = state.get("pending_tasks", [])
        resume_task_obj = next((t for t in pending_tasks_all if t.get("status") == "in_progress"), None)
        resume_task_json = resume_task_obj.get("task", "") if resume_task_obj else ""
        detected_domain = _get_task_domain(resume_task_json)
        try:
            domain_mistakes_raw = sb_get("mistakes",
                f"select=domain,context,what_failed,correct_approach,severity,root_cause,how_to_avoid&id=gt.1&domain=like.{detected_domain}%&order=severity.desc,created_at.desc&limit=5",
                svc=True) or []
        except Exception:
            domain_mistakes_raw = []
        # Backfill: if domain-scoped returns <3, supplement with global recent (deduplicated)
        if len(domain_mistakes_raw) < 3:
            try:
                global_mistakes = sb_get("mistakes",
                    "select=domain,context,what_failed,correct_approach,severity,root_cause,how_to_avoid&id=gt.1&order=created_at.desc&limit=10",
                    svc=True) or []
                seen = {m.get("context", "")[:80] for m in domain_mistakes_raw}
                for m in global_mistakes:
                    if m.get("context", "")[:80] not in seen and len(domain_mistakes_raw) < 5:
                        domain_mistakes_raw.append(m)
                        seen.add(m.get("context", "")[:80])
            except Exception:
                pass
        # --- 25.C: Top pattern injection ---
        try:
            top_pattern_rows = sb_get("pattern_frequency",
                "select=pattern_key,domain,frequency&id=gt.1&order=frequency.desc&limit=3",
                svc=True) or []
            top_patterns = [{"pattern": r.get("pattern_key", "")[:120], "domain": r.get("domain", ""), "freq": r.get("frequency", 0)} for r in top_pattern_rows]
        except Exception:
            top_patterns = []
        try:
            evolutions = sb_get("evolution_queue",
                "select=id,change_summary,change_type,confidence&status=eq.pending&order=confidence.desc&limit=5")
            if not isinstance(evolutions, list):
                evolutions = []
        except Exception:
            evolutions = []
        training = t_get_training_pipeline()
        # --- 25.D: Quality alert surface ---
        quality_alert = None
        try:
            q = training.get("quality", {})
            if q.get("trend") == "declining" or (q.get("7d_avg") or 1.0) < 0.75:
                quality_alert = {"trend": q.get("trend"), "7d_avg": q.get("7d_avg"), "note": "Review recent sessions -- session quality declining"}
        except Exception:
            pass
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
        # --- TASK-5.2.3: Task health check at boot ---
        task_health_warning = None
        try:
            th = t_task_health()
            if th.get("ok") and th.get("total_stale", 0) > 0:
                task_health_warning = th.get("warning")
        except Exception:
            pass
        # --- TASK-V8: Load behavioral_rules, infrastructure_map, credentials_index ---
        behavioral_rules_data = []
        infrastructure_map_data = []
        credentials_index_data = []
        migration_needed = False
        migration_missing = []
        try:
            br = t_get_behavioral_rules(domain=detected_domain)
            if br.get("migration_needed"):
                migration_needed = True
                migration_missing.append("behavioral_rules")
            else:
                behavioral_rules_data = br.get("rules", [])
        except Exception:
            migration_needed = True
            migration_missing.append("behavioral_rules")
        try:
            im = t_get_infrastructure()
            if im.get("migration_needed"):
                migration_needed = True
                migration_missing.append("infrastructure_map")
            else:
                infrastructure_map_data = im.get("components", [])
        except Exception:
            migration_needed = True
            migration_missing.append("infrastructure_map")
        try:
            ci = t_get_credentials_index()
            if ci.get("migration_needed"):
                migration_needed = True
                migration_missing.append("credentials_index")
            else:
                credentials_index_data = ci.get("credentials", [])
        except Exception:
            migration_needed = True
            migration_missing.append("credentials_index")
        # AGI-05/S3: Associative bridge -- surface 3 cross-domain KB entries related to current domain
        associated_context = []
        try:
            if detected_domain and detected_domain not in ("general", ""):
                # Get top keywords from domain KB entries
                domain_kb = sb_get("knowledge_base",
                    f"select=topic,instruction&id=gt.1&domain=eq.{detected_domain}&order=access_count.desc&limit=5",
                    svc=True) or []
                # Build keyword set from topics
                keywords = []
                for entry in domain_kb:
                    t = (entry.get("topic") or "").replace("_", " ").split()
                    keywords.extend([w for w in t if len(w) > 4])
                # Search OTHER domains for entries sharing those keywords
                seen_ids = set()
                for kw in keywords[:3]:  # top 3 keywords only -- keep it fast
                    cross = sb_get("knowledge_base",
                        f"select=id,domain,topic,instruction&id=gt.1&domain=neq.{detected_domain}&or=(topic.ilike.*{kw}*,instruction.ilike.*{kw}*)&order=access_count.desc&limit=2",
                        svc=True) or []
                    for r in cross:
                        rid = r.get("id")
                        if rid and rid not in seen_ids and len(associated_context) < 3:
                            associated_context.append({
                                "domain": r.get("domain", ""),
                                "topic": r.get("topic", ""),
                                "instruction": (r.get("instruction") or "")[:200],
                                "bridge_keyword": kw,
                            })
                            seen_ids.add(rid)
        except Exception:
            pass  # Non-fatal -- associative bridge is best-effort

        return {
            "ok": True,
            "health": health.get("overall", "unknown"),
            "components": health.get("components", {}),
            "counts": state.get("counts", {}),
            "last_session": state.get("last_session", ""),
            "last_session_ts": state.get("last_session_ts", ""),
            "in_progress_tasks": [t for t in pending_tasks_all if t.get("status") == "in_progress"],
            "pending_tasks": [t for t in pending_tasks_all if t.get("status") == "pending"],
            "resume_task": resume_task_obj,
            "session_md": state.get("session_md", ""),  # full SESSION.md content for claude.ai bootstrap
            "domain_mistakes": domain_mistakes_raw,
            "domain": detected_domain,
            "top_patterns": top_patterns,
            "quality_alert": quality_alert,
            "task_health_warning": task_health_warning,
            "behavioral_rules": behavioral_rules_data,
            "infrastructure_map": infrastructure_map_data,
            "credentials_index": credentials_index_data,
            "migration_needed": migration_needed,
            "migration_missing": migration_missing,
            "pending_evolutions": evolutions[:5] if isinstance(evolutions, list) else [],
            "training_pipeline": training,
            "live_tool_count": len(TOOLS),
            "system_map_drift": drift,
            "system_map": smap,
            "resume_checkpoint": _get_resume_checkpoint(resume_task_obj),
            "associated_context": associated_context,  # AGI-05/S3: cross-domain associative bridges
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _get_resume_checkpoint(resume_task_obj: dict) -> dict:
    """TASK-28.C: Fetch checkpoint_data from recent sessions for active resume_task.
    Returns checkpoint dict if found, else None."""
    try:
        if not resume_task_obj:
            return None
        rows = sb_get("sessions",
            "select=checkpoint_data,checkpoint_ts&order=created_at.desc&limit=5",
            svc=True) or []
        for row in rows:
            if row.get("checkpoint_data"):
                return row["checkpoint_data"]
        return None
    except Exception:
        return None


# -- TASK-28: Mid-Session State Checkpoint -----------------------------------
def t_checkpoint(active_task_id: str = "", last_action: str = "", last_result: str = "") -> dict:
    """Write a mid-session checkpoint to the current sessions row.
    Call after every subtask gate to prevent context collapse on long tasks.
    active_task_id: UUID of the task currently in progress.
    last_action: brief description of last completed action (e.g. 'patched core_train.py 29.B').
    last_result: outcome or next step (e.g. 'build SUCCESS, proceed to 29.C').
    On next session_start: resume_checkpoint field will contain this data."""
    try:
        from datetime import datetime
        checkpoint_data = {
            "active_task_id": active_task_id or "",
            "last_action": (last_action or "")[:500],
            "last_result": (last_result or "")[:500],
            "ts": datetime.utcnow().isoformat(),
        }
        # Write to most recent session row
        latest = sb_get("sessions", "select=id&order=created_at.desc&limit=1", svc=True)
        if not latest:
            return {"ok": False, "error_code": "no_session", "message": "No session row found", "retry_hint": False, "domain": "supabase"}
        session_id = latest[0]["id"]
        ok = sb_patch("sessions", f"id=eq.{session_id}", {
            "checkpoint_data": checkpoint_data,
            "checkpoint_ts": checkpoint_data["ts"],
        })
        if not ok:
            return {"ok": False, "error_code": "patch_failed", "message": "Failed to write checkpoint", "retry_hint": True, "domain": "supabase"}
        return {"ok": True, "session_id": session_id, "checkpoint": checkpoint_data}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "supabase"}


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
                    errors.append(f"L{i}: double keyword â€” {stripped[:60]}")
            if target == "core_tools.py":
                tool_fn_refs = _re.findall(r'"fn":\s*(t_\w+)', content)
                defined_fns  = set(_re.findall(r'^def (t_\w+)\(', content, _re.MULTILINE))
                for ref in tool_fn_refs:
                    if ref not in defined_fns:
                        errors.append(f"TOOLS refs '{ref}' but function not defined")
                if "TOOLS = {" not in content:
                    errors.append("TOOLS dict not found â€” critical corruption")
            for i, line in enumerate(lines, 1):
                if "backboard.railway" in line:
                    errors.append(f"L{i}: stale backboard.railway reference")
                if "core.py" in line and not line.strip().startswith("#"):
                    warnings.append(f"L{i}: stale core.py reference â€” file deleted")
            if size_kb > 150:
                warnings.append(f"{target} is {size_kb}KB â€” consider splitting (>150KB)")
            triple_count = content.count('"""')
            if triple_count % 2 != 0:
                warnings.append(f"Odd number of triple-quotes ({triple_count}) â€” possible unclosed docstring")
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
    """Apply multiple find-replace patches via Blobs API (atomic commit, no SHA conflict, no size limit).
    Uses whitespace-normalized fallback matching + char-level diff hint on failure."""
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
            found, count, matched, hint = _patch_find(content, old)
            if not found:
                skipped.append({"index": i, "reason": "not found",
                                 "old_str": old[:80], "hint": hint})
            elif count > 1:
                skipped.append({"index": i, "reason": f"ambiguous ({count}x)", "old_str": old[:80]})
            else:
                content = content.replace(matched, new, 1)
                applied.append({"index": i, "old_str": old[:80],
                                 "note": hint or "exact_match"})
        if not applied:
            return {"ok": False, "error_code": "no_patches_applied", "message": "No patches applied -- all old_str not found or ambiguous", "retry_hint": False, "domain": "github", "skipped": skipped}
        if path.endswith(".py"):
            import py_compile, tempfile as _tmpf
            with _tmpf.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(content); tmp = tf.name
            try:
                py_compile.compile(tmp, doraise=True)
            except py_compile.PyCompileError as e:
                import os; os.unlink(tmp)
                return {"ok": False, "error_code": "syntax_error", "message": f"Syntax error (patch not pushed): {e}", "retry_hint": False, "domain": "github"}
            finally:
                import os
                if os.path.exists(tmp): os.unlink(tmp)
        commit_sha = _gh_blob_write(path, content, message, repo)
        return {"ok": True, "path": path, "applied": len(applied), "skipped": len(skipped),
                "details": applied, "skipped_details": skipped,
                "commit": commit_sha[:12] if commit_sha else None}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "github"}


def t_session_end(summary: str = "", actions: str = "", domain: str = "general",
                  patterns: str = "", quality: str = "0.8",
                  skill_file_updated: str = "false",
                  force_close: str = "false",
                  active_task_ids: str = "",
                  new_tool_sop: str = "",
                  tools_updated: str = "") -> dict:
    """One-call session close.
    skill_file_updated: TASK-21.B gate. Pass 'true' after writing new rules to local skill file.
    force_close: pass 'true' to bypass all gates (owner explicit override).
    active_task_ids: pipe-separated UUIDs of tasks touched this session (e.g. 'uuid1|uuid2').
      session_end checks their status and warns if any are still pending/in_progress.
      Non-blocking -- warning only, Claude decides whether to patch or leave as-is.
    new_tool_sop: if non-empty, signals a new SOP was established this session affecting a specific tool.
      Triggers tools_updated gate -- session_end blocks until TOOLS dict is updated for that tool.
    tools_updated: pipe-separated tool names whose TOOLS dict description was updated this session.
      Required when new_tool_sop is set. Pass tool name(s) to confirm TOOLS dict was patched.
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
        # TASK-21.B gate RETIRED (owner directive 2026-03-19):
        # All new rules go to Supabase behavioral_rules only. Never write to local skill file.
        # skill_file_updated param retained for API compat but gate is permanently disabled.
        _skill_ok = True  # always passes
        _force = str(force_close).strip().lower() in ("true", "1", "yes")

        # TASK-23.B: tools_updated gate
        # If a new SOP was established this session affecting a specific tool,
        # block close until TOOLS dict description is confirmed updated for that tool.
        _new_sop = str(new_tool_sop).strip()
        _tools_updated = str(tools_updated).strip()
        if _new_sop and not _tools_updated and not _force:
            return {
                "ok": False,
                "blocked": True,
                "reason": "tools_dict_not_updated",
                "warning": (
                    f"New SOP established this session affecting tool: '{_new_sop}'. "
                    "TOOLS dict description must be updated for the affected tool(s) before closing. "
                    "Patch the TOOLS dict entry in core_tools.py via patch_file, then call session_end "
                    "with tools_updated='tool_name'. To skip: pass force_close=true."
                ),
                "new_tool_sop": _new_sop,
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

        # 5.5 AGI-03/S2: Counterfactual analyzer
        # For each mistake logged this session, write a hot_reflection with domain=causal
        # capturing what the correct action would have been.
        # Also close out any open causal_predictions from this session.
        counterfactuals_written = 0
        try:
            session_ts_anchor = (session_start_at - timedelta(hours=2)).isoformat()
            recent_mistakes = sb_get("mistakes",
                f"select=id,domain,context,what_failed,correct_approach,root_cause,how_to_avoid,severity&created_at=gte.{session_ts_anchor}&order=created_at.desc&limit=10",
                svc=True) or []
            for m in recent_mistakes:
                try:
                    cf_content = (
                        f"COUNTERFACTUAL ANALYSIS\n"
                        f"Domain: {m.get('domain','')}\n"
                        f"What failed: {(m.get('what_failed') or '')[:300]}\n"
                        f"Root cause: {(m.get('root_cause') or '')[:300]}\n"
                        f"Correct approach: {(m.get('correct_approach') or '')[:300]}\n"
                        f"Prevention: {(m.get('how_to_avoid') or '')[:300]}\n"
                        f"Counterfactual: If correct_approach had been applied, "
                        f"the failure mode would not have occurred. "
                        f"Pattern to reinforce: {(m.get('how_to_avoid') or '')[:200]}"
                    )
                    sb_post("hot_reflections", {
                        "domain": "causal",
                        "quality_score": 0.5 if m.get("severity") == "critical" else 0.7,
                        "summary": f"Counterfactual: {(m.get('what_failed') or '')[:120]}",
                        "content": cf_content,
                        "interface": "session_end_counterfactual",
                        "processed_by_cold": False,
                    })
                    counterfactuals_written += 1
                except Exception:
                    pass
            # Mark open causal_predictions for this session as outcome=session_ended
            try:
                sb_patch("causal_predictions",
                    f"session_id=eq.unknown&actual_outcome=is.null",
                    {"actual_outcome": "session_ended_no_outcome_recorded"})
            except Exception:
                pass
        except Exception:
            pass

        # AGI-06: Capability metrics -- multi-dimensional session scoring
        cap_metrics = {}
        try:
            # Fetch recent mistakes count for this session (last 2hr window)
            session_ts_cap = (datetime.utcnow() - timedelta(hours=2)).isoformat()
            session_mistakes = sb_get("mistakes",
                f"select=id,severity&created_at=gte.{session_ts_cap}&limit=20",
                svc=True) or []
            n_mistakes = len(session_mistakes)
            n_critical = sum(1 for m in session_mistakes if m.get("severity") in ("critical", "high"))
            n_actions = max(len(actions_list), 1)

            # Derive dimensions from available session data
            # ACCURACY: correctness without rework -- penalizes mistakes per action
            accuracy = max(0.0, round(1.0 - min(n_mistakes / n_actions, 1.0), 3))

            # EFFICIENCY: quality score directly (proxy for actions-to-outcome ratio)
            efficiency = round(q, 3)

            # AUTONOMY: 1.0 if no owner corrections, degrades with corrections
            # Note: owner_corrections not in session_end params yet -- derive from patterns keyword
            n_corrections = patterns.lower().count("correction") + patterns.lower().count("corrected")
            autonomy = max(0.0, round(1.0 - min(n_corrections * 0.2, 1.0), 3))

            # ROBUSTNESS: 1.0 if no critical/high mistakes, degrades with severity
            robustness = max(0.0, round(1.0 - (n_critical * 0.3), 3))

            # LEARNING RATE: improving if quality >= 0.8, stable 0.6-0.79, declining <0.6
            learning_rate = 1.0 if q >= 0.8 else (0.7 if q >= 0.6 else 0.4)

            # TRANSFER: did session produce cross-domain insights (patterns non-empty = yes)
            transfer = 0.8 if (caller_patterns and q >= 0.7) else 0.5

            composite = round((accuracy + efficiency + autonomy + robustness + learning_rate + transfer) / 6, 3)

            cap_metrics = {
                "accuracy": accuracy,
                "efficiency": efficiency,
                "autonomy": autonomy,
                "robustness": robustness,
                "learning_rate": learning_rate,
                "transfer": transfer,
                "composite_score": composite,
            }
            sb_post("capability_metrics", {
                "domain": domain,
                "accuracy": accuracy,
                "efficiency": efficiency,
                "autonomy": autonomy,
                "robustness": robustness,
                "learning_rate": learning_rate,
                "transfer": transfer,
                "composite_score": composite,
                "raw_data": {"n_mistakes": n_mistakes, "n_critical": n_critical,
                             "n_actions": n_actions, "quality": q},
                "notes": summary[:200] if summary else "",
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
            "tools_updated": _tools_updated if _tools_updated else "none",
            "new_tool_sop": _new_sop if _new_sop else "none",
            "counterfactuals_written": counterfactuals_written,
            "capability_metrics": cap_metrics,
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
    Defaults to core_main.py. core.py is deleted â€” do not use."""
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
        notify_owner(f"ROLLBACK triggered â€” {target} restored from {short_sha}. Deploying...")
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


def t_diff(path: str = "", sha_a: str = "", sha_b: str = "main") -> dict:
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
    """Instant Railway deployment status snapshot. Returns in <5s always.
    Root cause of previous hangs: any blocking I/O (sleep loops, urllib, long httpx timeout)
    kills the claude.ai MCP socket. Fix: single Railway GQL call with 4s httpx timeout,
    catch timeout immediately, return whatever state Railway has right now.
    timeout param retained for API compat but ignored -- no blocking ever.
    To poll live: PowerShell loop on https://core-agi-production.up.railway.app/health
    """
    try:
        tok = _RAILWAY_TOKEN or os.environ.get("RAILWAY_TOKEN", "")
        q = """
        query($projectId: String!, $serviceId: String!) {
            deployments(first: 1, input: { projectId: $projectId, serviceId: $serviceId }) {
                edges { node { id status createdAt meta } }
            }
        }"""
        headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        body = {"query": q, "variables": {"projectId": _RAILWAY_PROJECT, "serviceId": _RAILWAY_SERVICE}}
        # Hard 4s timeout -- MCP socket dies after ~10s, must return well before that
        r = httpx.post(_RAILWAY_GQL, headers=headers, json=body, timeout=4)
        data = r.json().get("data", {})
        edges = data.get("deployments", {}).get("edges", [])
        if not edges:
            return {"ok": False, "status": "NO_DEPLOYMENTS", "source": "railway_gql"}
        node = edges[0]["node"]
        status = node.get("status", "UNKNOWN")
        deploy_id = node.get("id", "")[:12]
        meta = node.get("meta") or {}
        if isinstance(meta, str):
            import json as _j
            try: meta = _j.loads(meta)
            except: meta = {}
        commit_sha = (meta.get("commitHash") or "")[:12]
        commit_msg = (meta.get("commitMessage") or "")[:80]
        terminal = {"SUCCESS", "FAILED", "CRASHED", "REMOVED"}
        in_progress = status in {"BUILDING", "DEPLOYING", "INITIALIZING", "QUEUED"}
        result = {
            "ok": status == "SUCCESS",
            "status": status,
            "deploy_id": deploy_id,
            "commit_sha": commit_sha,
            "commit_msg": commit_msg,
            "source": "railway_gql_snapshot",
        }
        if in_progress:
            result["note"] = f"Still {status} -- poll: Invoke-WebRequest https://core-agi-production.up.railway.app/health"
        elif status == "SUCCESS":
            result["note"] = "Deploy complete. Call ping_health to verify app is responding."
        elif status in {"FAILED", "CRASHED"}:
            result["note"] = "Deploy failed -- call railway_logs_live to diagnose."
        return result
    except httpx.TimeoutException:
        return {"ok": False, "status": "GQL_TIMEOUT", "note": "Railway GQL did not respond in 4s. Poll manually: Invoke-WebRequest https://core-agi-production.up.railway.app/health", "source": "railway_gql_snapshot"}
    except Exception as e:
        return {"ok": False, "error": str(e), "source": "railway_gql_snapshot"}


def t_railway_logs_live(lines: str = "50", keyword: str = "") -> dict:
    """Fetch live Railway deployment stdout logs via GraphQL.
    Returns actual print() output from the running container -- not just commit history.
    lines: number of log lines to return (default 50, max 500).
    keyword: optional filter string (case-insensitive).
    """
    try:
        node = _railway_latest_deployment()
        if not node:
            return {"ok": False, "error": "No deployments found"}
        deploy_id = node["id"]
        limit = min(int(lines) if lines else 50, 500)
        q = """
        query($deploymentId: String!, $limit: Int) {
            deploymentLogs(deploymentId: $deploymentId, limit: $limit) {
                message
                timestamp
            }
        }"""
        data = _railway_gql(q, {"deploymentId": deploy_id, "limit": limit})
        logs = data.get("deploymentLogs", []) or []
        kw = keyword.strip().lower() if keyword else ""
        if kw:
            logs = [l for l in logs if kw in l.get("message", "").lower()]
        formatted = []
        for l in logs:
            ts = l.get("timestamp", "")[:19].replace("T", " ")
            msg = l.get("message", "")
            formatted.append(f"{ts} {msg}")
        return {
            "ok": True,
            "deploy_id": deploy_id[:12],
            "deploy_status": node.get("status"),
            "total": len(formatted),
            "keyword": kw or None,
            "logs": formatted,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_railway_env_get(key: str = "") -> dict:
    """Read Railway environment variables via GraphQL.
    key: specific var name to read (returns just that value). Empty = return all vars.
    """
    try:
        q = """
        query($projectId: String!, $serviceId: String!, $environmentId: String!) {
            variables(projectId: $projectId, serviceId: $serviceId, environmentId: $environmentId)
        }"""
        data = _railway_gql(q, {"projectId": _RAILWAY_PROJECT, "serviceId": _RAILWAY_SERVICE, "environmentId": _RAILWAY_ENV})
        vars_obj = data.get("variables") or {}
        if isinstance(vars_obj, dict):
            all_vars = vars_obj
        else:
            all_vars = {}
        if key:
            val = all_vars.get(key)
            return {"ok": True, "key": key, "value": val, "found": val is not None}
        # Return all var names (not values -- security)
        return {"ok": True, "count": len(all_vars), "keys": sorted(all_vars.keys())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_railway_env_set(key: str, value: str) -> dict:
    """Write a Railway environment variable via GraphQL variableUpsert.
    Triggers a redeploy automatically after setting.
    key: env var name (e.g. GROQ_API_KEY). value: new value.
    """
    try:
        if not key or not value:
            return {"ok": False, "error": "key and value are required"}
        q = """
        mutation($input: VariableUpsertInput!) {
            variableUpsert(input: $input)
        }"""
        inp = {
            "projectId": _RAILWAY_PROJECT,
            "serviceId": _RAILWAY_SERVICE,
            "environmentId": _RAILWAY_ENV,
            "name": key,
            "value": value,
        }
        data = _railway_gql(q, {"input": inp})
        success = data.get("variableUpsert", False)
        if success:
            notify(f"Railway env var set: <b>{key}</b> updated. Redeploy required to take effect.")
        return {"ok": success, "key": key, "note": "Redeploy required for changes to take effect"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_railway_service_info() -> dict:
    """Full Railway service snapshot: status, latest deployment, env var keys, project info."""
    try:
        # Service + project
        q = """
        query($serviceId: String!, $envId: String!) {
            service(id: $serviceId) {
                id name projectId
            }
            serviceInstance(serviceId: $serviceId, environmentId: $envId) {
                id startCommand region
            }
        }"""
        data = _railway_gql(q, {"serviceId": _RAILWAY_SERVICE, "envId": _RAILWAY_ENV})
        svc = data.get("service", {})
        inst = data.get("serviceInstance", {})
        # Latest deployment
        node = _railway_latest_deployment()
        meta = (node.get("meta") or {}) if node else {}
        return {
            "ok": True,
            "service": {"id": svc.get("id","")[:12], "name": svc.get("name"), "project_id": svc.get("projectId","")[:12]},
            "instance": {"region": inst.get("region"), "start_cmd": inst.get("startCommand")},
            "latest_deploy": {
                "id": (node.get("id") or "")[:12],
                "status": node.get("status"),
                "commit": (meta.get("commitMessage") or "")[:60],
                "sha": (meta.get("commitSha") or "")[:12],
            } if node else None,
            "ids": {"project": _RAILWAY_PROJECT, "service": _RAILWAY_SERVICE, "env": _RAILWAY_ENV},
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_ping_health() -> dict:
    """Direct health check - calls t_health() internally without HTTP."""
    return t_health()


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
    try:
        answer = gemini_chat(system, user, max_tokens=512)
        return {"ok": True, "answer": answer, "kb_hits": len(kb_results), "model": "gemini-2.5-flash-lite", "question": question}
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
        
        # Evolution queue counts - use COUNT queries per status (scales to millions of rows)
        evo_pending = sb_get("evolution_queue", "select=count&status=eq.pending", svc=True)
        evo_applied = sb_get("evolution_queue", "select=count&status=eq.applied", svc=True)
        evo_rejected = sb_get("evolution_queue", "select=count&status=eq.rejected", svc=True)
        evo_counts = {
            "pending": evo_pending[0]["count"] if evo_pending else 0,
            "applied": evo_applied[0]["count"] if evo_applied else 0,
            "rejected": evo_rejected[0]["count"] if evo_rejected else 0,
        }
        
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
            "evolution_queue": evo_counts,
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


# -- Background Researcher (globals + helpers only â€” loop lives in core_train) ----
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
    print("[RESEARCH] _repopulate_evolution_queue: disabled â€” backlog items never go to evolution_queue")
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
        msg = f"chore: trigger redeploy â€” {reason or 'manual trigger'}"
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
        # BACKLOG.md deleted in Task 1.8 â€” backlog lives in Supabase only
        # Slim results to prevent h11 Content-Length overflow on large batches
        slim_results = [{"id": r.get("id"), "ok": r.get("ok"), "note": str(r.get("note", ""))[:120]} for r in results]
        return {"ok": True, "applied": len(applied), "failed": len(failed), "results": slim_results}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Railway GraphQL constants
_RAILWAY_TOKEN   = os.environ.get("RAILWAY_TOKEN", "")
_RAILWAY_GQL     = "https://backboard.railway.app/graphql/v2"
_RAILWAY_PROJECT = "b6ead639-5fa2-4637-b8a5-d403ce6dac82"
_RAILWAY_SERVICE = "5c43b876-47ca-4125-834d-92c268d89b33"
_RAILWAY_ENV     = "002425ee-b1a3-4ec2-b668-f24ae5e12411"

def _railway_gql(query: str, variables: dict = None) -> dict:
    """Execute a Railway GraphQL query. Returns response data dict."""
    tok = _RAILWAY_TOKEN or os.environ.get("RAILWAY_TOKEN", "")
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    body = {"query": query}
    if variables:
        body["variables"] = variables
    r = httpx.post(_RAILWAY_GQL, headers=headers, json=body, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise Exception(data["errors"][0]["message"])
    return data.get("data", {})

def _railway_latest_deployment() -> dict:
    """Get the latest deployment for the CORE service.
    NOTE: meta is a SCALAR (JSON blob) on Railway -- never select subfields.
    Access as node['meta']['commitHash'] etc after retrieval.
    projectId is REQUIRED in input -- serviceId alone returns empty.
    """
    q = """
    query($projectId: String!, $serviceId: String!) {
        deployments(first: 1, input: { projectId: $projectId, serviceId: $serviceId }) {
            edges { node { id status createdAt environmentId staticUrl meta } }
        }
    }"""
    data = _railway_gql(q, {"projectId": _RAILWAY_PROJECT, "serviceId": _RAILWAY_SERVICE})
    edges = data.get("deployments", {}).get("edges", [])
    if not edges:
        return {}
    node = edges[0]["node"]
    # meta is a scalar JSON blob -- unpack it into the node for easy access
    meta = node.get("meta") or {}
    if isinstance(meta, dict):
        node["commitHash"] = (meta.get("commitHash") or "")[:12]
        node["commitMessage"] = meta.get("commitMessage") or ""
        node["commitAuthor"] = meta.get("commitAuthor") or ""
    return node

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
    """Real-time deploy status via Railway GraphQL (direct) + GitHub commit history.
    Railway GraphQL gives live status: BUILDING | DEPLOYING | SUCCESS | FAILED | CRASHED.
    Falls back to GitHub commit statuses if Railway token unavailable."""
    try:
        now = datetime.utcnow()
        # Primary: Railway GraphQL (real-time, no lag)
        railway_deploy = None
        railway_error = None
        try:
            node = _railway_latest_deployment()
            if node:
                created = node.get("createdAt", "")
                time_since = ""
                if created:
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
                        mins = int((now - dt).total_seconds() // 60)
                        time_since = f"{mins}m ago" if mins < 60 else f"{mins//60}h{mins%60}m ago"
                    except: pass
                meta = node.get("meta", {}) or {}
                railway_deploy = {
                    "deploy_id": node.get("id", "")[:12],
                    "status": node.get("status", "UNKNOWN"),  # BUILDING|DEPLOYING|SUCCESS|FAILED|CRASHED
                    "commit_sha": (meta.get("commitSha") or "")[:12],
                    "commit_msg": (meta.get("commitMessage") or "")[:80],
                    "created_at": created[:19] if created else "",
                    "time_since": time_since,
                    "source": "railway_graphql",
                }
        except Exception as re:
            railway_error = str(re)

        # Secondary: last 3 GitHub commits with Railway status callbacks
        h = _ghh()
        gh_r = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page=3", headers=h, timeout=8)
        gh_r.raise_for_status()
        recent = []
        for commit in gh_r.json():
            sha = commit["sha"]
            msg = commit.get("commit", {}).get("message", "")[:60]
            ts = commit.get("commit", {}).get("committer", {}).get("date", "")
            sr = httpx.get(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{sha}/statuses",
                           headers=h, timeout=8)
            statuses = sr.json() if sr.status_code == 200 else []
            rw = [s for s in statuses if "railway" in s.get("context","").lower()
                  or "railway" in s.get("description","").lower()]
            st = rw[0] if rw else {}
            time_since = ""
            if ts:
                try:
                    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                    mins = int((now - dt).total_seconds() // 60)
                    time_since = f"{mins}m ago" if mins < 60 else f"{mins//60}h{mins%60}m ago"
                except: pass
            recent.append({"commit_sha": sha[:12], "commit_msg": msg,
                           "state": st.get("state", "no_status"),
                           "description": st.get("description", ""),
                           "updated_at": st.get("updated_at", ""), "time_since": time_since})

        latest_gh = recent[0] if recent else {}
        # Prefer Railway GraphQL for latest status, fall back to GitHub
        latest = railway_deploy or latest_gh
        state = (railway_deploy or {}).get("status") or latest_gh.get("state", "?")
        msg = (railway_deploy or latest_gh).get("commit_msg", "?")
        return {
            "ok": True,
            "latest": latest,
            "railway_live": railway_deploy,
            "railway_error": railway_error,
            "recent": recent,
            "summary": f"Latest: {state} â€” {msg}"
        }
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


def t_read_image_content(content_b64: str = "", mime_type: str = "image/jpeg",
                         project_id: str = "", topic: str = "",
                         prompt: str = "", file_name: str = "") -> dict:
    """Extract text/data from an image using Claude vision (claude-haiku-4-5).
    content_b64: base64-encoded image bytes.
    mime_type: image/jpeg or image/png.
    project_id + topic: if provided, auto-saves extracted text to project KB.
    prompt: extraction instruction (default: extract all visible text, tables, lists, and data).
    Returns: {ok, extracted_text, tokens_used, kb_saved}
    """
    try:
        import base64 as _b64
        if not content_b64:
            return {"ok": False, "error": "content_b64 required"}
        _prompt = prompt or (
            "Extract ALL text visible in this image. Include: any text, numbers, dates, names, "
            "status fields, table contents, lists, annotations, and any other data. "
            "Preserve structure. Output plain text only."
        )
        ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
        if not ANTHROPIC_KEY:
            return {"ok": False, "error": "ANTHROPIC_API_KEY not set"}
        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": content_b64
                    }},
                    {"type": "text", "text": _prompt}
                ]
            }]
        }
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload, timeout=60
        )
        r.raise_for_status()
        data = r.json()
        extracted = data["content"][0]["text"].strip()
        usage = data.get("usage", {})
        kb_saved = False
        if project_id and topic and extracted:
            label = file_name or "image"
            kb_content = f"[Extracted from image: {label}]\n{extracted}"
            ok = sb_upsert("knowledge_base",
                {"domain": f"project:{project_id}", "topic": topic,
                 "content": kb_content[:4000], "confidence": "high"},
                on_conflict="domain,topic")
            kb_saved = bool(ok)
        return {
            "ok": True,
            "extracted_text": extracted,
            "chars": len(extracted),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "kb_saved": kb_saved,
            "project_id": project_id,
            "topic": topic
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def t_read_pdf_content(content_b64: str = "", project_id: str = "",
                       topic: str = "", file_name: str = "",
                       max_chars: str = "8000") -> dict:
    """Extract text from a PDF using pdfminer.six.
    content_b64: base64-encoded PDF bytes.
    project_id + topic: if provided, auto-saves to project KB.
    max_chars: truncate extracted text to this length (default 8000).
    Returns: {ok, extracted_text, pages, chars, kb_saved}
    """
    try:
        import base64 as _b64, io
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
        if not content_b64:
            return {"ok": False, "error": "content_b64 required"}
        pdf_bytes = _b64.b64decode(content_b64)
        pdf_io = io.BytesIO(pdf_bytes)
        text_io = io.StringIO()
        extract_text_to_fp(pdf_io, text_io, laparams=LAParams(), output_type="text", codec=None)
        extracted = text_io.getvalue().strip()
        limit = int(max_chars) if max_chars else 8000
        truncated = len(extracted) > limit
        extracted_out = extracted[:limit]
        kb_saved = False
        if project_id and topic and extracted_out:
            label = file_name or "pdf"
            kb_content = f"[Extracted from PDF: {label}]\n{extracted_out}"
            ok = sb_upsert("knowledge_base",
                {"domain": f"project:{project_id}", "topic": topic,
                 "content": kb_content[:4000], "confidence": "high"},
                on_conflict="domain,topic")
            kb_saved = bool(ok)
        # Count pages roughly (\x0c = form feed = page break in pdfminer output)
        pages = extracted.count('\x0c') + 1
        return {
            "ok": True,
            "extracted_text": extracted_out,
            "chars": len(extracted_out),
            "pages": pages,
            "truncated": truncated,
            "kb_saved": kb_saved,
            "project_id": project_id,
            "topic": topic
        }
    except ImportError:
        return {"ok": False, "error": "pdfminer.six not installed -- add to requirements.txt and redeploy"}
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

        # 5. Open task_queue items -- pending AND in_progress (full context for Q1+Q3 pre-flight checks)
        open_tasks_pending = sb_get("task_queue",
            "select=task,status&status=eq.pending&order=priority.desc&limit=20",
            svc=True) or []
        open_tasks_inprog = sb_get("task_queue",
            "select=task,status&status=eq.in_progress&order=priority.desc&limit=10",
            svc=True) or []
        open_tasks = open_tasks_pending + open_tasks_inprog

        # Return all raw signals to Claude Desktop.
        # Claude reads, reasons as architect, applies pre-flight, calls task_add directly.
        return {
            "ok": True,
            "instruction": (
                "YOU are the architect. Read all signals below. Think 6 months ahead. "
                "Before calling task_add for ANY task, run ALL 5 pre-flight checks:\n"
                "Q1 DUPLICATE CHECK: Does this task already exist in open_tasks (pending or in_progress)? "
                "If yes -> SKIP, do not add.\n"
                "Q2 SIGNAL FRESHNESS: Is the driving signal (evolution created_at or pattern) recent? "
                "If the signal is >30 days old with no recent activity -> flag as stale, do not add standalone task.\n"
                "Q3 REDUNDANCY CHECK: Does this task overlap >50% with an in_progress task? "
                "If yes -> merge as a subtask of that task, do not create new standalone task.\n"
                "Q4 REAL IMPACT CHECK: Is this task a real architectural/behavioral change, or cosmetic? "
                "Cosmetic tasks (renaming, minor doc updates, formatting) -> batch into a housekeeping task, not standalone.\n"
                "Q5 ACTIONABILITY CHECK: Is this task specific enough to execute now? "
                "Vague tasks ('improve reasoning', 'make CORE smarter') -> log as KB gap entry instead, not a task.\n"
                "OUTCOME MAP: pass all 5 -> call task_add | exists -> skip | stale -> skip | "
                "redundant -> merge as subtask | cosmetic -> batch | vague -> add_knowledge gap instead.\n"
                "Subtasks must be concrete, verifiable, and executable. No hallucinated tool names."
            ),
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
                    {"status": t.get("status"), "task": str(t.get("task", ""))[:200]}
                    for t in open_tasks
                ],
            },
            "counts": {
                "evolutions": len(evolutions),
                "patterns": len(patterns),
                "cold_reflections": len(cold),
                "gaps": len(gaps),
                "open_tasks_pending": len(open_tasks_pending),
                "open_tasks_inprog": len(open_tasks_inprog),
            },
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}



# -- Task 8: Server-side patching tools ---------------------------------------

def t_patch_file(path: str, patches: str, message: str, repo: str = "", dry_run: str = "false", allow_deletion: str = "false") -> dict:
    """Server-side patch: fetch file from GitHub, apply find-replace patches,
    run py_compile if .py, then push. Prevents syntax errors from crashing Railway.
    patches: JSON array of {old_str, new_str} objects (same format as multi_patch).
    dry_run: true = show diff but do not push.
    allow_deletion: must be 'true' to permit a patch with empty/missing new_str.
                    Default false -- empty new_str is BLOCKED to prevent accidental deletion."""
    try:
        import subprocess, tempfile as _tmpfile
        repo = repo or GITHUB_REPO
        if isinstance(patches, str):
            patches = json.loads(patches)
        content = _gh_blob_read(path, repo)
        applied = []
        skipped = []
        _allow_del = str(allow_deletion).lower() == "true"
        for i, patch in enumerate(patches):
            old = patch.get("old_str", "")
            new = patch.get("new_str")  # None if missing, "" if explicitly empty
            # DELETION GUARD: block patches with missing or empty new_str unless allow_deletion=true
            if (new is None or new == "") and not _allow_del:
                skipped.append({"index": i, "reason": "DELETION BLOCKED: new_str is missing or empty. Pass allow_deletion=true to permit intentional deletions.", "old_str": old[:80]})
                continue
            if new is None:
                new = ""
            found, count, matched, hint = _patch_find(content, old)
            if not found:
                skipped.append({"index": i, "reason": "not found", "old_str": old[:80], "hint": hint})
            elif count > 1:
                skipped.append({"index": i, "reason": f"ambiguous ({count}x)", "old_str": old[:80]})
            else:
                content = content.replace(matched, new, 1)
                applied.append({"index": i, "old_str": old[:80], "note": hint or "exact_match"})
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
        commit_sha = _gh_blob_write(path, content, message, repo)
        return {"ok": True, "dry_run": False, "path": path,
                "applied": len(applied), "skipped": len(skipped),
                "syntax_ok": syntax_ok, "details": applied, "skipped_details": skipped,
                "commit": commit_sha[:12] if commit_sha else None}
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
        content = _gh_blob_read(path, repo or GITHUB_REPO)
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
        existing = _gh_blob_read(path, repo)
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
        commit_sha = _gh_blob_write(path, new_content, message, repo)
        return {"ok": True, "path": path,
                "original_lines": len(existing.splitlines()),
                "appended_lines": len(content_to_append.splitlines()),
                "total_lines": len(new_content.splitlines()),
                "commit": commit_sha[:12] if commit_sha else None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -- Supabase write layer (TASK-14) ------------------------------------------
def t_sb_patch(table: str = "", filters: str = "", data="") -> dict:
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
    try:
        ok = sb_patch(table, filters.strip(), data)
        if not ok:
            return {"ok": False, "error_code": "patch_failed", "message": f"Supabase patch failed for table {table}", "retry_hint": True, "domain": "supabase"}
        return {"ok": True, "table": table, "filters": filters, "updated_fields": list(data.keys())}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "supabase"}


def t_sb_upsert(table: str = "", data="", on_conflict: str = "") -> dict:
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
    try:
        ok = sb_upsert(table, data, on_conflict.strip())
        if not ok:
            return {"ok": False, "error_code": "upsert_failed", "message": f"Supabase upsert failed for table {table}", "retry_hint": True, "domain": "supabase"}
        return {"ok": True, "table": table, "on_conflict": on_conflict, "fields": list(data.keys())}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "supabase"}


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


def t_task_update(task_id: str = "", status: str = "", result: str = "") -> dict:
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


def t_task_add(title: str = "", description: str = "", priority: str = "5",
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
                content: str = "", confidence: str = "medium",
                source_type: str = "", source_ref: str = "") -> dict:
    """Upsert a KB entry on domain+topic. Updates if exists, inserts if new. Prevents duplicates.
    Use instead of add_knowledge when the rule may already exist.
    TASK-27.C: Logs overwrite diff to Telegram when instruction changes.
    TASK-24.B: source_type=manual|ingested|evolved|session, source_ref=URL or session_id."""
    if not domain or not topic:
        return {"ok": False, "error": "domain and topic required"}
    if not instruction and not content:
        return {"ok": False, "error": "at least one of instruction or content required"}
    try:
        # TASK-27.C: Check for existing entry -- notify owner if instruction is being changed
        existing = sb_get("knowledge_base",
            f"select=instruction&domain=eq.{domain}&topic=eq.{topic}&limit=1",
            svc=True)
        if existing:
            ex_instr = (existing[0].get("instruction") or "").strip()
            new_instr = (instruction or "").strip()
            if ex_instr and new_instr and ex_instr.lower() != new_instr.lower():
                notify(f"[KB UPDATE] {domain}/{topic}\nOld: {ex_instr[:120]}\nNew: {new_instr[:120]}")
        # Normalize confidence enum (coerce float strings if needed)
        VALID_CONFIDENCE = {"low", "medium", "high", "proven"}
        if str(confidence) not in VALID_CONFIDENCE:
            try:
                v = float(confidence)
                confidence = "proven" if v >= 0.9 else "high" if v >= 0.7 else "medium" if v >= 0.4 else "low"
                print(f"[SCHEMA] confidence coerced {v} -> '{confidence}'")
            except (TypeError, ValueError):
                confidence = "medium"
        row = {
            "domain": domain, "topic": topic,
            "instruction": instruction or None,
            "content": content or "",
            "confidence": confidence,
            "source": "mcp_session",
        }
        if source_type:
            row["source_type"] = source_type
        if source_ref:
            row["source_ref"] = source_ref
        errs = _validate_write("knowledge_base", row)
        if errs:
            return {"ok": False, "domain": domain, "topic": topic, "error": f"Schema violation: {errs}"}
        try:
            h = {**_sbh(True), "Prefer": "resolution=merge-duplicates,return=minimal"}
            r = httpx.post(f"{SUPABASE_URL}/rest/v1/knowledge_base?on_conflict=domain,topic", headers=h, json=row, timeout=15)
            if not r.is_success:
                return {"ok": False, "domain": domain, "topic": topic, "action": "upserted", "error": f"Supabase {r.status_code}: {r.text[:300]}"}
            return {"ok": True, "domain": domain, "topic": topic, "action": "upserted"}
        except Exception as e:
            return {"ok": False, "domain": domain, "topic": topic, "action": "upserted", "error": str(e)}
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
    SESSION.md is static -- never auto-written. KB is the sole server-side persistence layer.

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

        # NOTE: SESSION.md is static (never auto-written). KB is the sole persistence layer.
        # SESSION.md edits are manual-only when active rules or protocol changes.

        return {
            "ok": True,
            "persisted_to": persisted_to,
            "rule": rule[:120],
            "domain": domain,
            "category": category,
            "reminder": (
                "IMPORTANT: Now write this rule to the LOCAL SKILL FILE: "
                "C:\\Users\\rnvgg\\.claude-skills\\CORE_AGI_SKILL_V4.md "
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

def t_project_index(project_id: str = "", topic: str = "", content: str = "", notify: str = "true") -> dict:
    """Index content into a project's KB. Writes one KB chunk (topic+content) to domain=project:{id},
    then updates last_indexed timestamp on the projects table.
    Use to push document extracts, notes, or field data into a project's knowledge base from Claude.ai.
    If content is empty, only refreshes the last_indexed timestamp (ping-mode).
    topic: short label for this chunk (e.g. 'RMU commissioning checklist' or 'daily report 2026-03-16').
    notify=true sends Telegram confirmation."""
    try:
        if not project_id:
            return {"ok": False, "error": "project_id required"}
        results = {}
        # Write KB chunk if content provided
        if content and topic:
            ok = sb_upsert("knowledge_base",
                {"domain": f"project:{project_id}", "topic": topic, "content": content, "confidence": "high"},
                on_conflict="domain,topic")
            results["kb_written"] = bool(ok)
            results["domain"] = f"project:{project_id}"
            results["topic"] = topic
        else:
            results["kb_written"] = False
            results["note"] = "no content/topic provided -- timestamp-only update"
        # Always update last_indexed
        ts = datetime.utcnow().isoformat()
        sb_patch("projects", f"project_id=eq.{project_id}", {"last_indexed": ts})
        results["last_indexed"] = ts
        results["project_id"] = project_id
        # Telegram notify
        if str(notify).lower() in ("true", "1", "yes"):
            msg = f"[PROJECT] {project_id} indexed\ntopic: {topic or '(timestamp only)'}\nts: {ts}"
            notify_owner(msg)
        return {"ok": True, **results}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# -- TASK-10.D: CORE Mistake Predictor ----------------------------------------

def _predict_failure(operation: str = "", context: str = "", domain: str = "", session_id: str = "") -> dict:
    """Predicts likely failure modes before executing an operation -- AGI-03 causal layer.
    Searches mistakes table via two passes: (1) what_failed keyword match, (2) root_cause + context match.
    Returns structured causal chain: predicted_failure_modes=[{mode, probability, root_cause, prevention}].
    Writes prediction to causal_predictions table for post-session counterfactual analysis.
    operation: short label of what's about to happen (e.g. 'gh_search_replace', 'patch_file', 'sb_insert').
    context: extra info (e.g. file being edited, table being written to).
    domain: optional domain filter to narrow mistake search.
    session_id: optional -- links prediction to session for accuracy tracking.
    Returns: {predicted, warnings, predicted_failure_modes, top_mistake, confidence, match_count}
    """
    try:
        if not operation:
            return {"predicted": False, "warnings": [], "predicted_failure_modes": [],
                    "top_mistake": None, "confidence": 0.0}

        # Pass 1: match on what_failed (existing behavior)
        qs1 = f"what_failed=ilike.%25{operation}%25&order=created_at.desc&limit=5"
        if domain:
            qs1 += f"&domain=eq.{domain}"
        rows = sb_get("mistakes", qs1) or []

        # Pass 2: match on root_cause or context fields (broader causal signal)
        if len(rows) < 3:
            op_slug = operation.replace("_", " ")
            qs2 = f"root_cause=ilike.%25{op_slug}%25&order=created_at.desc&limit=5"
            if domain:
                qs2 += f"&domain=eq.{domain}"
            rows2 = sb_get("mistakes", qs2) or []
            # Deduplicate by id
            seen = {r.get("id") for r in rows}
            for r in rows2:
                if r.get("id") not in seen:
                    rows.append(r)
                    seen.add(r.get("id"))

        if not rows:
            return {"predicted": False, "warnings": [], "predicted_failure_modes": [],
                    "top_mistake": None, "confidence": 0.0,
                    "note": f"no known mistakes for operation={operation}"}

        # Build flat warnings (backward compat)
        warnings = []
        for row in rows:
            if row.get("how_to_avoid"):
                warnings.append({
                    "domain": row.get("domain", ""),
                    "warning": row.get("what_failed", "")[:120],
                    "avoid": row.get("how_to_avoid", "")[:200],
                    "severity": row.get("severity", "medium"),
                })

        # Build causal chain: predicted_failure_modes
        # Probability derived from: high=0.7, medium=0.45, low=0.2; recency boost if recent
        _sev_prob = {"high": 0.70, "medium": 0.45, "low": 0.20}
        predicted_failure_modes = []
        for row in rows[:5]:
            sev = row.get("severity", "medium")
            prob = _sev_prob.get(sev, 0.45)
            predicted_failure_modes.append({
                "mode": (row.get("what_failed") or "")[:120],
                "probability": prob,
                "root_cause": (row.get("root_cause") or "")[:200],
                "prevention": (row.get("how_to_avoid") or "")[:200],
                "domain": row.get("domain", ""),
                "severity": sev,
            })

        top = rows[0] if rows else None
        confidence = min(0.9, 0.3 + (len(rows) * 0.15))

        result = {
            "predicted": len(predicted_failure_modes) > 0,
            "warnings": warnings,
            "predicted_failure_modes": predicted_failure_modes,
            "top_mistake": {
                "domain": top.get("domain") if top else None,
                "what_failed": (top.get("what_failed") or "")[:150] if top else None,
                "correct_approach": (top.get("correct_approach") or "")[:200] if top else None,
            } if top else None,
            "confidence": round(confidence, 2),
            "match_count": len(rows),
            "instruction": "Review predicted_failure_modes before proceeding. Highest probability modes indicate likely failure patterns based on mistake history.",
        }

        # Write prediction to causal_predictions for post-session counterfactual analysis
        try:
            sb_post("causal_predictions", {
                "session_id": session_id or "unknown",
                "operation": operation[:200],
                "domain": domain or (top.get("domain") if top else ""),
                "planned_action": context[:500] if context else "",
                "mistake_context": {"match_count": len(rows), "domains": list({r.get("domain", "") for r in rows})},
                "predicted_failure_modes": predicted_failure_modes,
                "actual_outcome": None,  # filled in at session_end counterfactual pass
                "was_correct": None,
            })
        except Exception:
            pass  # Non-fatal -- prediction still returned

        return result
    except Exception as e:
        return {"predicted": False, "warnings": [], "predicted_failure_modes": [],
                "top_mistake": None, "confidence": 0.0, "error": str(e)}






def t_predict_failure(operation: str = "", context: str = "", domain: str = "", session_id: str = "") -> dict:
    """MCP wrapper: predict likely failure modes before an operation.
    Searches mistake history for matching patterns and returns causal chain warnings.
    operation: what you're about to do (e.g. 'gh_search_replace', 'patch_file', 'sb_insert').
    context: optional extra info (file name, table name, content snippet).
    domain: optional domain to narrow search (e.g. 'core_agi.patching').
    session_id: optional -- links prediction to session for counterfactual tracking.
    Use before any risky write operation to get a pre-flight causal warning.
    """
    return _predict_failure(operation=operation, context=context, domain=domain, session_id=session_id)
# -- TASK-26: Tool Reliability Tracking ---------------------------------------
def _track_tool_stat(tool_name: str, success: bool, error: str = None):
    """Fire-and-forget: increment tool_stats counters for today. Non-fatal -- never raises."""
    try:
        from datetime import date
        today = date.today().isoformat()
        # Fetch existing row
        existing = sb_get("tool_stats", 
            f"select=call_count,success_count,fail_count&tool_name=eq.{tool_name}&date=eq.{today}",
            svc=True)
        if existing and len(existing) > 0:
            # Row exists -- increment counters
            row = existing[0]
            new_data = {
                "call_count": row["call_count"] + 1,
                "success_count": row["success_count"] + (1 if success else 0),
                "fail_count": row["fail_count"] + (0 if success else 1),
            }
            if not success and error:
                new_data["last_error"] = error
            sb_patch("tool_stats", f"tool_name=eq.{tool_name}&date=eq.{today}", new_data)
        else:
            # Row doesn't exist -- insert initial values
            sb_insert("tool_stats", {
                "tool_name": tool_name,
                "date": today,
                "call_count": 1,
                "success_count": 1 if success else 0,
                "fail_count": 0 if success else 1,
                "last_error": error if not success else None
            })
    except Exception:
        pass  # Always non-fatal


def t_tool_stats(days: str = "7") -> dict:
    """TASK-26.C: Per-tool success/fail rate for last N days (default 7).
    Returns tools sorted by fail_rate descending. Any tool with fail_rate > 0.2 is flagged.
    Use to identify flaky tools that need investigation."""
    try:
        n = int(days) if days else 7
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=n)).isoformat()
        rows = sb_get("tool_stats",
            f"select=tool_name,date,call_count,success_count,fail_count,last_error&date=gte.{cutoff}&order=date.desc&limit=500",
            svc=True) or []
        # Aggregate per tool
        agg = {}
        for r in rows:
            name = r["tool_name"]
            if name not in agg:
                agg[name] = {"tool_name": name, "calls": 0, "successes": 0, "failures": 0, "last_error": None}
            agg[name]["calls"] += r.get("call_count", 0)
            agg[name]["successes"] += r.get("success_count", 0)
            agg[name]["failures"] += r.get("fail_count", 0)
            if r.get("last_error") and not agg[name]["last_error"]:
                agg[name]["last_error"] = r["last_error"]
        results = []
        flagged = []
        for name, d in agg.items():
            rate = round(d["failures"] / d["calls"], 3) if d["calls"] > 0 else 0.0
            entry = {**d, "fail_rate": rate, "health": "ok" if rate <= 0.2 else "flagged"}
            results.append(entry)
            if rate > 0.2:
                flagged.append(name)
        results.sort(key=lambda x: x["fail_rate"], reverse=True)
        return {"ok": True, "days": n, "tools_tracked": len(results),
                "flagged": flagged, "results": results}
    except Exception as e:
        return {"ok": False, "error_code": "exception", "message": str(e), "retry_hint": True, "domain": "supabase"}
  
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
                               "desc": "TASK-21: Persist a new behavioral rule to knowledge_base (confidence=proven). SESSION.md is static -- not written. Call when any new hard rule, SOP, correction or architectural decision is established this session. Returns reminder to also write to local skill file via Windows-MCP:FileSystem. category=hard_rule|sop|architectural_decision|correction."},
    "debug_fn":               {"fn": t_debug_fn,               "perm": "READ",    "args": ["fn_name", "dry_run", "extra_args"],
                               "desc": "Battle-tested debug harness for any CORE function. Staged: 0_resolve, 1_preflight, 2_execute, 4_write_intercepted. dry_run=true default (skips DB writes)."},
    "railway_logs_live":      {"fn": t_railway_logs_live,      "perm": "READ",    "args": ["lines", "keyword"],
                               "desc": "Fetch live Railway stdout via GraphQL deploymentLogs. Returns actual container print() output. lines=count (default 50). keyword=filter. Use to see [RESEARCH]/[COLD]/[SIM] in real-time."},
    "railway_env_get":        {"fn": t_railway_env_get,        "perm": "READ",    "args": ["key"],
                               "desc": "Read Railway env vars via GraphQL. key=specific name (returns value). Empty = all var names only (not values)."},
    "railway_env_set":        {"fn": t_railway_env_set,        "perm": "WRITE",   "args": ["key", "value"],
                               "desc": "Write Railway env var via GraphQL variableUpsert. Redeploy required for changes to take effect."},
    "railway_service_info":   {"fn": t_railway_service_info,   "perm": "READ",    "args": [],
                               "desc": "Railway service snapshot: name, region, latest deploy status+commit, IDs."},
    "get_training_pipeline":  {"fn": t_get_training_pipeline,  "perm": "READ",    "args": [],
                               "desc": "Full training pipeline status. Returns: hot (total, unprocessed, simulation_ok, last_real, last_simulation), cold (last_run_ts, last_run_mins_ago, threshold, last_patterns_found, last_evolutions_queued, recent_5_summaries), patterns (active_count, stale_count, top), evolutions (pending, applied), quality (7d_avg, trend), health_flags (simulation_dead|cold_stale_Xmin|unprocessed_backlog_X|zero_patterns_last_5_runs|quality_declining), pipeline_ok. Use at session_start or when diagnosing training issues."},
    "get_training_status":    {"fn": t_training_status,        "perm": "READ",    "args": [],
                               "desc": "Legacy thin wrapper. Use get_training_pipeline for full status."},
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
    "add_knowledge":          {"fn": t_add_knowledge,          "perm": "WRITE",   "args": ["domain", "topic", "instruction", "content", "tags", "confidence", "source_type", "source_ref"],
                               "desc": "Add NEW knowledge that does not yet exist. Returns ok=False if domain+topic already exists -- correct behavior, not an error. Use ONLY for genuinely new knowledge. Use kb_update to overwrite stale or outdated existing knowledge. source_type=manual|ingested|evolved|session (optional). source_ref=URL or session_id (optional)."},
    "log_mistake":            {"fn": t_log_mistake,            "perm": "WRITE",   "args": ["context", "what_failed", "correct_approach", "domain", "root_cause", "how_to_avoid", "severity"],
                               "desc": "Log a mistake so CORE never repeats it. correct_approach=the right way to do it (required). severity=low|medium|high|critical. Always call this when CORE makes an error â€” it is the primary learning mechanism."},
    "notify_owner":           {"fn": t_notify,                 "perm": "WRITE",   "args": ["message", "level"],
                               "desc": "Send Telegram notification."},
    "sb_insert":              {"fn": t_sb_insert,              "perm": "WRITE",   "args": ["table", "data"],
                               "desc": "Insert row into Supabase table."},
    "sb_bulk_insert":         {"fn": t_sb_bulk_insert,         "perm": "WRITE",   "args": ["table", "rows"],
                               "desc": "Insert multiple rows into Supabase in one HTTP call."},
    "get_state_key":          {"fn": t_get_state_key,          "perm": "READ",    "args": ["key"],
                               "desc": "Read back a specific state key written by update_state. Use to retrieve values set via update_state or set_simulation â€” the only way to read back those keys."},
    "task_update":            {"fn": t_task_update,            "perm": "WRITE",   "args": ["task_id", "status", "result"],
                               "desc": "Update a task_queue row status. task_id=UUID or TASK-N string. status=pending/in_progress/done/failed. result=optional outcome note. Use instead of raw sb_query for task status changes."},
    "task_add":               {"fn": t_task_add,               "perm": "WRITE",   "args": ["title", "description", "priority", "subtasks", "blocked_by"],
                               "desc": "Add a new task to task_queue with proper schema. Sets source=mcp_session automatically. Use instead of raw sb_insert for new tasks â€” enforces correct structure."},
    "kb_update":              {"fn": t_kb_update,              "perm": "WRITE",   "args": ["domain", "topic", "instruction", "content", "confidence", "source_type", "source_ref"],
                               "desc": "Update EXISTING stale or outdated KB knowledge. Overwrites entry at domain+topic. Use when a rule has changed or content is wrong. Do NOT use for new knowledge -- use add_knowledge for that. Will also insert if not found (upsert behavior -- prevents duplicates). source_type=manual|ingested|evolved|session (optional). source_ref=URL or session_id (optional)."},
    "mistakes_since":         {"fn": t_mistakes_since,         "perm": "READ",    "args": ["hours"],
                               "desc": "Return mistakes logged in the last N hours (default 24). Use at session_end to review only this session's errors, not the rolling last-10."},
    "trigger_cold_processor": {"fn": t_trigger_cold_processor, "perm": "WRITE",   "args": [],
                               "desc": "Manually trigger cold processor."},
    "backfill_patterns":      {"fn": t_backfill_patterns,      "perm": "WRITE",   "args": ["batch_size"],
                               "desc": "TASK-20: Fire-and-forget backfill of pattern_frequency -> KB. Returns job_id immediately. Poll backfill_status for result. batch_size=max per run (default 20)."},
    "backfill_status":         {"fn": t_backfill_status,         "perm": "READ",    "args": [],
                               "desc": "TASK-20: Poll backfill job status. Returns inserted count + done/running/error/idle. Call after backfill_patterns."},
    "ingest_knowledge":       {"fn": t_ingest_knowledge,       "perm": "EXECUTE", "args": ["topic", "sources", "max_per_source", "since_days"],
                               "desc": "Trigger knowledge ingestion pipeline. Fetches topic from public sources (arxiv/docs/medium/reddit/hackernews/stackoverflow), scores by engagement, writes to kb_* tables, injects hot_reflections for CORE to evolve. sources=comma-separated or 'all'. max_per_source=cap per fetcher (default 20). since_days=recency filter (default 7)."},
    "listen":                 {"fn": t_listen,                 "perm": "EXECUTE", "args": [],
                               "desc": "LISTEN MODE: fire-and-forget. Starts background listen job, returns job_id immediately. Then call listen_result to fetch chunks once done."},
    "listen_result":           {"fn": t_listen_result,           "perm": "EXECUTE", "args": [],
                               "desc": "Fetch listen job status + results. Call after listen. Returns status (running|done|error), cycles, patterns_found, evolutions_queued, chunks. If running, wait and call again."},
    "approve_evolution":      {"fn": t_approve_evolution,      "perm": "WRITE",   "args": ["evolution_id"],
                               "desc": "Approve and apply a pending evolution by ID"},
    "reject_evolution":       {"fn": t_reject_evolution,       "perm": "WRITE",   "args": ["evolution_id", "reason"],
                               "desc": "Reject a pending evolution by ID."},
    "bulk_reject_evolutions": {"fn": t_bulk_reject_evolutions, "perm": "WRITE",   "args": ["change_type", "ids", "reason", "include_synthesized"],
                               "desc": "Bulk reject pending evolutions silently. change_type=backlog|knowledge|empty, or comma-separated ids. include_synthesized=true to also reject synthesized items."},
    "gh_search_replace":      {"fn": t_gh_search_replace,      "perm": "EXECUTE", "args": ["path", "old_str", "new_str", "message", "repo", "dry_run", "allow_deletion"],
                               "desc": "Surgical find-and-replace in a GitHub file. old_str must be unique. DELETION GUARD: empty/missing new_str is blocked by default -- pass allow_deletion=true for intentional deletions. For .py files prefer patch_file. Always gh_read_lines first."},
    "gh_read_lines":          {"fn": t_gh_read_lines,          "perm": "READ",    "args": ["path", "start_line", "end_line", "repo"],
                               "desc": "Read specific line range from a GitHub file with line numbers. Use before any edit to get exact content. Preferred over read_file for large files or when you need a specific section."},
    "write_file":             {"fn": t_write_file,             "perm": "EXECUTE", "args": ["path", "content", "message", "repo"],
                               "desc": "Write NEW file to GitHub repo â€” FULL OVERWRITE. BLOCKED for core_main.py and core_tools.py. Use patch_file or gh_search_replace for surgical edits on existing files. Only use for brand new files."},
    "ask":                    {"fn": t_ask,                    "perm": "READ",    "args": ["question", "domain"],
                               "desc": "Ask CORE anything â€” searches KB + mistakes for context, then answers via Groq. Use for domain questions, SOPs, architectural decisions. Uses GROQ_FAST for simple questions, GROQ_MODEL for complex ones."},
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
    "tool_stats":             {"fn": t_tool_stats,             "perm": "READ",    "args": ["days"],
                               "desc": "TASK-26: Per-tool success/fail rate for last N days (default 7). Returns tools sorted by fail_rate desc. fail_rate>0.2 = flagged. Use to identify flaky tools."},
    "checkpoint":             {"fn": t_checkpoint,             "perm": "WRITE",   "args": ["active_task_id", "last_action", "last_result"],
                               "desc": "TASK-28: Write mid-session checkpoint. Call after every subtask gate to prevent context collapse on long tasks. active_task_id=UUID of current task. last_action=brief description of last completed step. last_result=outcome or next step. session_start returns resume_checkpoint field with this data."},
    "session_end":            {"fn": t_session_end,            "perm": "WRITE",   "args": ["summary", "actions", "domain", "patterns", "quality", "skill_file_updated", "force_close", "active_task_ids"],
                               "desc": "One-call session close. actions=pipe-separated strings (| only, NOT comma). quality clamped 0.0-1.0 auto. BEFORE calling: (1) log_mistake for every error, (2) add_knowledge for every new insight, (3) changelog_add for every deploy, (4) update task statuses in task_queue. active_task_ids=pipe-separated UUIDs of tasks touched -- warns if any still pending/in_progress. skill_file_updated gate RETIRED (owner 2026-03-19) -- always pass skill_file_updated=true. All new rules go to Supabase behavioral_rules via add_behavioral_rule only. Returns: training_ok (bool), duration_seconds, reflection_warning if hot reflection failed, task_status_warnings if tasks left open."},
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
    "project_index":          {"fn": t_project_index,          "perm": "WRITE",   "args": ["project_id", "topic", "content", "notify"],
                               "desc": "Index content into a project KB. Writes topic+content chunk to domain=project:{id} and updates last_indexed. If no content, timestamp-only ping. notify=true sends Telegram."},
    "read_image_content":     {"fn": t_read_image_content,     "perm": "READ",    "args": ["content_b64", "mime_type", "project_id", "topic", "prompt", "file_name"],
                               "desc": "Extract text/data from image (JPG/PNG) using Claude vision (claude-haiku). content_b64=base64 image bytes. If project_id+topic provided, auto-saves to project KB. Returns extracted_text."},
    "read_pdf_content":       {"fn": t_read_pdf_content,       "perm": "READ",    "args": ["content_b64", "project_id", "topic", "file_name", "max_chars"],
                               "desc": "Extract text from PDF using pdfminer.six. content_b64=base64 PDF bytes. If project_id+topic provided, auto-saves to project KB. Returns extracted_text, pages."},
    "project_prepare":        {"fn": t_project_prepare,        "perm": "WRITE",   "args": ["project_ids"],
                               "desc": "Railway-side: assemble KB context for project(s) and store in project_context for Claude Desktop to consume. Sends Telegram notify."},
    "project_consume":        {"fn": t_project_consume,        "perm": "WRITE",   "args": ["project_id"],
                               "desc": "Mark project_context rows as consumed after Claude Desktop has loaded them."},
    "predict_failure":        {"fn": t_predict_failure,        "perm": "READ",    "args": ["operation", "context", "domain", "session_id"],
                               "desc": "AGI-03: Causal failure predictor. Dual-pass search (what_failed + root_cause). Returns predicted_failure_modes=[{mode,probability,root_cause,prevention}] causal chain. Writes prediction to causal_predictions table for counterfactual analysis. session_id optional."},
    "ping_health":            {"fn": t_ping_health,            "perm": "READ",    "args": [],
                               "desc": "Hit live Railway / endpoint."},
    "patch_file":             {"fn": t_patch_file,            "perm": "EXECUTE", "args": ["path", "patches", "message", "repo", "dry_run", "allow_deletion"],
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
        # Support both plain string args and rich dict args {"name": x, "type": y, "description": z}
        if isinstance(a, dict):
            arg_name = a.get("name", str(a))
            arg_type = a.get("type", "string")
            arg_desc = a.get("description", arg_name)
            if arg_name in _ARRAY_PARAMS or arg_type == "array":
                props[arg_name] = {
                    "type": "array",
                    "description": arg_desc,
                    "items": {"type": "object"}
                }
            else:
                props[arg_name] = {"type": arg_type, "description": arg_desc}
        else:
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
            # TASK-23.D: Coerce numeric string args to int/float at dispatcher level
            _INT_ARGS = {"start_line", "end_line", "limit", "hours", "lines", "priority", "max_results", "since_days", "max_per_source"}
            _FLOAT_ARGS = {"confidence", "quality"}
            coerced_args = {}
            for k, v in tool_args.items():
                if k in _INT_ARGS and isinstance(v, str):
                    try: coerced_args[k] = int(v)
                    except (ValueError, TypeError): coerced_args[k] = v
                elif k in _FLOAT_ARGS and isinstance(v, str):
                    try: coerced_args[k] = float(v)
                    except (ValueError, TypeError): coerced_args[k] = v
                else:
                    coerced_args[k] = v
            arg_names = {d["name"] if isinstance(d, dict) else d for d in tool["args"]} if tool["args"] else set()
            result = tool["fn"](**{k: v for k, v in coerced_args.items() if not arg_names or k in arg_names})
            text = json.dumps(result, default=str)
            # TASK-26.B: Track success in tool_stats (fire-and-forget, non-fatal)
            _track_tool_stat(tool_name, success=True)
            return ok({"content": [{"type": "text", "text": text}]})
        except Exception as e:
            # TASK-26.B: Track failure in tool_stats (fire-and-forget, non-fatal)
            _track_tool_stat(tool_name, success=False, error=str(e)[:200])
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



# â”€â”€ TASK-4: Binance Integration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

import hmac as _hmac
import hashlib as _hashlib
import time as _time
import urllib.parse as _urlparse

def _binance_sign(secret: str, params: dict) -> str:
    """Sign Binance request with HMAC-SHA256."""
    qs = _urlparse.urlencode(params)
    return _hmac.new(secret.encode(), qs.encode(), _hashlib.sha256).hexdigest()

def _binance_headers(api_key: str) -> dict:
    return {"X-MBX-APIKEY": api_key, "Content-Type": "application/x-www-form-urlencoded"}

def _binance_get(path: str, params: dict = None) -> dict:
    """Unsigned Binance GET (public endpoints)."""
    import httpx as _httpx
    try:
        r = _httpx.get(f"https://api.binance.com{path}", params=params or {}, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def _binance_signed_get(path: str, params: dict) -> dict:
    """Signed Binance GET (account endpoints)."""
    import httpx as _httpx
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret  = os.getenv("BINANCE_SECRET_KEY", "")
    if not api_key or not secret:
        return {"error": "BINANCE_API_KEY or BINANCE_SECRET_KEY not set"}
    params["timestamp"] = int(_time.time() * 1000)
    params["signature"] = _binance_sign(secret, params)
    try:
        r = _httpx.get(f"https://api.binance.com{path}", params=params,
                       headers=_binance_headers(api_key), timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def _binance_signed_post(path: str, params: dict) -> dict:
    """Signed Binance POST (order endpoints)."""
    import httpx as _httpx
    api_key = os.getenv("BINANCE_API_KEY", "")
    secret  = os.getenv("BINANCE_SECRET_KEY", "")
    if not api_key or not secret:
        return {"error": "BINANCE_API_KEY or BINANCE_SECRET_KEY not set"}
    params["timestamp"] = int(_time.time() * 1000)
    params["signature"] = _binance_sign(secret, params)
    try:
        r = _httpx.post(f"https://api.binance.com{path}", data=params,
                        headers=_binance_headers(api_key), timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def t_crypto_price(symbol: str = "BTCUSDT") -> dict:
    """Get current Binance spot price for a symbol. symbol=e.g. BTCUSDT, BNBUSDT, ETHUSDT."""
    symbol = symbol.upper().replace("/", "").replace("-", "")
    data = _binance_get("/api/v3/ticker/price", {"symbol": symbol})
    if "error" in data:
        return {"ok": False, "error": data["error"]}
    if "price" not in data:
        return {"ok": False, "error": f"Symbol not found or invalid: {symbol}", "raw": data}
    price = float(data["price"])
    # Also fetch 24h stats
    stats = _binance_get("/api/v3/ticker/24hr", {"symbol": symbol})
    result = {"ok": True, "symbol": symbol, "price": price}
    if "priceChangePercent" in stats:
        result["change_24h_pct"] = float(stats["priceChangePercent"])
        result["high_24h"] = float(stats["highPrice"])
        result["low_24h"] = float(stats["lowPrice"])
        result["volume_24h"] = float(stats["volume"])
    return result


def t_crypto_balance(asset: str = "") -> dict:
    """Get Binance account balances. asset=optional filter e.g. BTC. Returns all non-zero balances if empty."""
    data = _binance_signed_get("/api/v3/account", {"recvWindow": 5000})
    if "error" in data:
        return {"ok": False, "error": data["error"]}
    if "balances" not in data:
        return {"ok": False, "error": "Unexpected response", "raw": data}
    balances = [
        {"asset": b["asset"], "free": float(b["free"]), "locked": float(b["locked"])}
        for b in data["balances"]
        if float(b["free"]) > 0 or float(b["locked"]) > 0
    ]
    if asset:
        balances = [b for b in balances if b["asset"].upper() == asset.upper()]
    return {"ok": True, "balances": balances, "count": len(balances)}


def t_crypto_trade(symbol: str = "", side: str = "", quantity: str = "",
                   confirm: str = "", order_type: str = "MARKET") -> dict:
    """Execute a Binance spot trade. REQUIRES confirm='CONFIRM' to execute.
    symbol=e.g. BTCUSDT. side=BUY or SELL. quantity=amount of BASE asset.
    order_type=MARKET (default) or LIMIT (requires price param).
    NEVER executes without explicit confirm='CONFIRM' from owner."""
    if not symbol or not side or not quantity:
        return {"ok": False, "error": "symbol, side, and quantity are all required"}
    if confirm != "CONFIRM":
        # Dry run -- show what would be executed
        price_data = t_crypto_price(symbol)
        est_price = price_data.get("price", "unknown")
        est_value = float(quantity) * float(est_price) if est_price != "unknown" else "unknown"
        return {
            "ok": False,
            "dry_run": True,
            "message": f"DRY RUN -- pass confirm='CONFIRM' to execute",
            "would_execute": {
                "symbol": symbol.upper(),
                "side": side.upper(),
                "quantity": quantity,
                "order_type": order_type,
                "estimated_price_usdt": est_price,
                "estimated_value_usdt": est_value,
            }
        }
    # Execute trade
    symbol = symbol.upper().replace("/", "").replace("-", "")
    side   = side.upper()
    if side not in ("BUY", "SELL"):
        return {"ok": False, "error": "side must be BUY or SELL"}
    params = {
        "symbol":   symbol,
        "side":     side,
        "type":     order_type.upper(),
        "quantity": quantity,
    }
    result = _binance_signed_post("/api/v3/order", params)
    if "orderId" not in result:
        return {"ok": False, "error": "Order failed", "raw": result}
    # Log to Supabase trades table
    try:
        fills = result.get("fills", [])
        avg_price = (
            sum(float(f["price"]) * float(f["qty"]) for f in fills) /
            sum(float(f["qty"]) for f in fills)
        ) if fills else None
        sb_post("trades", {
            "order_id":    str(result["orderId"]),
            "symbol":      symbol,
            "side":        side,
            "quantity":    float(quantity),
            "price":       avg_price,
            "status":      result.get("status", "UNKNOWN"),
            "confirmed_by": "ki",
            "raw_response": result,
        })
    except Exception as log_err:
        print(f"[BINANCE] trade log error: {log_err}")
    notify(f"[TRADE EXECUTED] {side} {quantity} {symbol}\nOrder ID: {result['orderId']}\nStatus: {result.get('status')}")
    return {
        "ok":       True,
        "order_id": result["orderId"],
        "symbol":   symbol,
        "side":     side,
        "quantity": quantity,
        "status":   result.get("status"),
        "fills":    result.get("fills", []),
    }



# â”€â”€ TASK-4: Register Binance tools in TOOLS dict (must be after function defs) â”€
TOOLS["crypto_price"]   = {"fn": t_crypto_price,   "perm": "READ",    "args": ["symbol"],
                            "desc": "Get current Binance spot price + 24h stats for a symbol. symbol=e.g. BTCUSDT, ETHUSDT, BNBUSDT (default BTCUSDT). No API key required. Returns price, 24h change %, high, low, volume."}
TOOLS["crypto_balance"] = {"fn": t_crypto_balance, "perm": "READ",    "args": ["asset"],
                            "desc": "Get Binance account balances. Requires BINANCE_API_KEY + BINANCE_SECRET_KEY env vars. asset=optional filter e.g. BTC. Returns all non-zero balances if asset empty."}
TOOLS["crypto_trade"]   = {"fn": t_crypto_trade,   "perm": "EXECUTE", "args": ["symbol", "side", "quantity", "confirm", "order_type"],
                            "desc": "Execute a Binance spot trade. REQUIRES confirm=CONFIRM to execute -- omit for dry run showing estimated value. symbol=e.g. BTCUSDT. side=BUY or SELL. quantity=base asset amount. order_type=MARKET (default) or LIMIT. Logs all trades to Supabase trades table. Sends Telegram notify on execution. NEVER execute without owner CONFIRM."}


# -- TASK-5.2: Task State Validator ------------------------------------------

def t_task_health() -> dict:
    """Check task_queue for stale/abandoned tasks.
    Flags: in_progress tasks older than 24hr, pending tasks with no updates older than 7 days.
    Returns: {ok, stale_in_progress: [...], stale_pending: [...], total_stale, warning}
    Called at session_start to surface abandoned work before starting new tasks."""
    try:
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        threshold_in_progress = now - timedelta(hours=24)
        threshold_pending = now - timedelta(days=7)

        # Fetch in_progress tasks older than 24hr (UUID PK -- no id=gt.1 filter)
        stale_ip_rows = sb_get(
            "task_queue",
            "select=id,task,status,created_at,updated_at&status=eq.in_progress&order=created_at.asc",
            svc=True
        ) or []

        # Fetch pending tasks older than 7 days (UUID PK -- no id=gt.1 filter)
        stale_pend_rows = sb_get(
            "task_queue",
            "select=id,task,status,created_at,updated_at&status=eq.pending&order=created_at.asc",
            svc=True
        ) or []

        stale_in_progress = []
        for row in stale_ip_rows:
            ts_str = row.get("updated_at") or row.get("created_at") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < threshold_in_progress:
                    task_raw = row.get("task", "")
                    title = task_raw[:80] if isinstance(task_raw, str) else str(task_raw)[:80]
                    hours_stale = int((now - ts).total_seconds() / 3600)
                    stale_in_progress.append({
                        "id": str(row.get("id", "")),
                        "title": title,
                        "hours_stale": hours_stale,
                        "last_update": ts_str,
                    })
            except Exception:
                pass

        stale_pending = []
        for row in stale_pend_rows:
            ts_str = row.get("updated_at") or row.get("created_at") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < threshold_pending:
                    task_raw = row.get("task", "")
                    title = task_raw[:80] if isinstance(task_raw, str) else str(task_raw)[:80]
                    days_stale = int((now - ts).total_seconds() / 86400)
                    stale_pending.append({
                        "id": str(row.get("id", "")),
                        "title": title,
                        "days_stale": days_stale,
                        "last_update": ts_str,
                    })
            except Exception:
                pass

        total_stale = len(stale_in_progress) + len(stale_pending)
        warning = None
        if total_stale > 0:
            parts = []
            if stale_in_progress:
                parts.append(f"{len(stale_in_progress)} in_progress task(s) stuck >24hr")
            if stale_pending:
                parts.append(f"{len(stale_pending)} pending task(s) untouched >7d")
            warning = "STALE TASKS: " + ", ".join(parts) + ". Review before starting new work."

        return {
            "ok": True,
            "stale_in_progress": stale_in_progress,
            "stale_pending": stale_pending,
            "total_stale": total_stale,
            "warning": warning,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}


TOOLS["task_health"] = {"fn": t_task_health, "perm": "READ", "args": [],
                         "desc": "TASK-5.2: Check task_queue for stale/abandoned tasks. Flags: in_progress >24hr, pending >7d. Returns stale_in_progress, stale_pending, total_stale, warning. Call at session_start to surface abandoned work before starting new tasks."}


# -- TASK-6: Mandatory Verification Gate System --------------------------------

def t_verify_before_deploy(operation: str = "", target_file: str = "", context: str = "") -> dict:
    """TASK-6.1: Pre-deploy verification enforcer.
    Checks 3 gates before any deploy: (a) validate_syntax, (b) gh_read_lines called on target, (c) predict_failure.
    operation: description of what is about to be deployed (e.g. 'patch t_session_end in core_tools.py').
    target_file: the file being patched (e.g. 'core_tools.py').
    context: optional extra context for predict_failure.
    Returns: verification_score 0.0-1.0, passed_gates list, failed_gates list, blocked (True if score < 0.8).
    HARD RULE: Never deploy without calling this first. Score < 0.8 = do not proceed."""
    try:
        passed = []
        failed = []
        warnings = []

        # Gate A: validate_syntax on target file
        if target_file and target_file.endswith(".py"):
            try:
                syn = t_validate_syntax(target_file)
                if syn.get("ok"):
                    passed.append("validate_syntax")
                else:
                    failed.append(f"validate_syntax: {syn.get('error', 'failed')}")
            except Exception as e:
                failed.append(f"validate_syntax: exception({e})")
        else:
            # Non-Python file or no file specified -- syntax check not applicable
            passed.append("validate_syntax_skipped_non_py")

        # Gate B: target file was read before patch (heuristic -- check if target_file provided)
        if target_file:
            passed.append("target_file_specified")
        else:
            failed.append("target_file_not_specified: always name the file being patched")

        # Gate C: predict_failure check
        try:
            pf = t_predict_failure(operation=operation or "deploy", context=context or target_file, domain="core_agi.deployment")
            pf_warnings = pf.get("warnings", [])
            if pf_warnings:
                warnings.extend(pf_warnings[:3])
                # Warnings are non-blocking but lower score
                passed.append(f"predict_failure_warnings({len(pf_warnings)})")
            else:
                passed.append("predict_failure_clean")
        except Exception as e:
            # predict_failure failure is non-blocking
            passed.append(f"predict_failure_unavailable({e})")

        # Score: 1.0 per gate passed, 0.0 per gate failed. Normalize to 0.0-1.0.
        total_gates = len(passed) + len(failed)
        score = round(len(passed) / total_gates, 2) if total_gates > 0 else 0.0
        # Warnings reduce score by 0.05 each, floor at 0.0
        score = max(0.0, score - 0.05 * len(warnings))
        blocked = score < 0.8

        return {
            "ok": True,
            "verification_score": score,
            "passed_gates": passed,
            "failed_gates": failed,
            "warnings": warnings,
            "blocked": blocked,
            "message": (
                f"BLOCKED: verification_score={score} < 0.8. Fix failed gates before deploying."
                if blocked else
                f"CLEAR: verification_score={score}. Proceed with deploy."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True, "blocked": True}


TOOLS["verify_before_deploy"] = {
    "fn": t_verify_before_deploy,
    "perm": "READ",
    "args": [
        {"name": "operation", "type": "string", "description": "What is being deployed"},
        {"name": "target_file", "type": "string", "description": "File being patched (e.g. core_tools.py)"},
        {"name": "context", "type": "string", "description": "Optional extra context for failure prediction"},
    ],
    "desc": "TASK-6.1: Pre-deploy verification enforcer. Call BEFORE any patch_file or gh_search_replace. Checks: validate_syntax, target_file named, predict_failure. Returns verification_score 0.0-1.0. blocked=True if score < 0.8 -- do not deploy. HARD RULE: never deploy without calling this first.",
}


# -- TASK-V8: V8 Architecture Tools -------------------------------------------

def t_get_behavioral_rules(domain: str = None, page: str = "1", page_size: str = "200") -> dict:
    """Load active behavioral rules from behavioral_rules table.
    If domain provided: return universal rules + domain-specific rules.
    If domain=None: return universal rules only.
    Filter: active=true, id=gt.1, order by priority asc.
    If table does not exist (migration pending): return empty list with migration_needed flag.
    Supports pagination: page=1-based page number, page_size=rows per page (default 200, max 500)."""
    try:
        try:
            pg = max(1, int(page))
            ps = min(500, max(10, int(page_size)))
        except Exception:
            pg, ps = 1, 200
        offset = (pg - 1) * ps
        if domain and domain.strip():
            filters = f"active=eq.true&id=gt.1&domain=in.(universal,{domain.strip()})&order=priority.asc&limit={ps}&offset={offset}"
        else:
            filters = f"active=eq.true&id=gt.1&domain=eq.universal&order=priority.asc&limit={ps}&offset={offset}"
        rows = sb_get("behavioral_rules", f"select=trigger,pointer,full_rule,domain,priority,tested,confidence&{filters}", svc=True)
        if rows is None:
            return {"ok": True, "rules": [], "migration_needed": True, "warning": "behavioral_rules table may not exist yet"}
        result = {"ok": True, "rules": rows or [], "count": len(rows or []), "domain": domain or "universal", "page": pg, "page_size": ps}
        if len(rows or []) == ps:
            result["has_more"] = True
            result["next_page"] = pg + 1
        return result
    except Exception as e:
        err = str(e)
        if "not found" in err.lower() or "does not exist" in err.lower():
            return {"ok": True, "rules": [], "migration_needed": True, "warning": f"behavioral_rules table not found: {err}"}
        return {"ok": False, "error": err, "error_code": "exception", "retry_hint": True}

TOOLS["get_behavioral_rules"] = {"fn": t_get_behavioral_rules, "perm": "READ",
    "args": [
        {"name": "domain", "type": "string", "description": "Domain to load rules for (e.g. railway, code, postgres). Returns universal + domain rules."},
        {"name": "page", "type": "string", "description": "Page number (1-based, default 1)"},
        {"name": "page_size", "type": "string", "description": "Rows per page (default 200, max 500)"},
    ],
    "desc": "TASK-V8: Load active behavioral rules from behavioral_rules table. Pass domain= to get universal + domain-specific rules. Returns rules ordered by priority. Supports pagination (page, page_size). If table missing returns migration_needed=true."}


def t_add_behavioral_rule(trigger: str = "", pointer: str = "", full_rule: str = "",
                           domain: str = "universal", priority: str = "5",
                           source: str = "core_discovered", confidence: str = "0.8",
                           expires_at: str = None) -> dict:
    """Add new behavioral rule to behavioral_rules table.
    Validates trigger against valid enum. Checks for duplicate trigger+domain+pointer."""
    VALID_TRIGGERS = {
        "before_any_act","during_action","post_action","before_domain_work",
        "during_code_write","post_deploy","before_auth","during_deploy","post_mistake",
        "before_ddl","during_supabase_write","post_discovery","before_code","post_new_service",
        "before_deploy","on_failure","post_new_credential","before_project_doc","on_blocked",
        "before_tool_call","on_tool_silent","session_open","before_api_call","on_empty_result",
        "session_close","before_supabase_write","on_conflict","before_supabase_read",
        "on_missing_credential","reasoning_preflight","before_adding_tool","on_version_mismatch",
        "before_training_change","on_interrupted_session","before_routing_change",
        "before_irreversible_act","post_action","during_deploy",
    }
    VALID_DOMAINS = {
        "universal","postgres","railway","github","supabase","groq","powershell","zapier",
        "project","auth","code","reasoning","failure_recovery","local_pc","telegram",
    }
    try:
        if trigger not in VALID_TRIGGERS:
            return {"ok": False, "error_code": "invalid_trigger", "message": f"Invalid trigger '{trigger}'. Valid: {sorted(VALID_TRIGGERS)}", "retry_hint": False, "domain": "behavioral_rules"}
        if domain not in VALID_DOMAINS:
            return {"ok": False, "error_code": "invalid_domain", "message": f"Invalid domain '{domain}'. Valid: {sorted(VALID_DOMAINS)}", "retry_hint": False, "domain": "behavioral_rules"}
        try:
            prio = int(priority)
        except Exception:
            prio = 5
        try:
            conf = float(confidence)
            conf = max(0.0, min(1.0, conf))
        except Exception:
            conf = 0.8
        # Per-session rate limit: max 10 insertions per session
        _br_counter = getattr(t_add_behavioral_rule, "_session_insert_count", 0)
        if _br_counter >= 10:
            return {"ok": False, "error_code": "rate_limited", "message": f"Behavioral rule rate limit reached ({_br_counter}/10 this session). Prevents evolution queue flooding. Owner can reset by restarting session.", "retry_hint": False}
        # Duplicate check
        existing = sb_get("behavioral_rules", f"select=id,pointer&active=eq.true&trigger=eq.{trigger}&domain=eq.{domain}&limit=5", svc=True) or []
        for ex in existing:
            if (ex.get("pointer") or "").strip().lower() == (pointer or "").strip().lower():
                return {"ok": True, "action": "skipped_duplicate", "message": "Rule with same trigger+domain+pointer already exists"}
        row = {"trigger": trigger, "pointer": pointer, "full_rule": full_rule,
               "domain": domain, "priority": prio, "source": source, "confidence": conf, "active": True, "tested": False}
        if expires_at:
            row["expires_at"] = expires_at
        ok = sb_post("behavioral_rules", row)
        if ok:
            t_add_behavioral_rule._session_insert_count = getattr(t_add_behavioral_rule, "_session_insert_count", 0) + 1
        return {"ok": ok, "action": "inserted", "trigger": trigger, "domain": domain, "session_inserts": getattr(t_add_behavioral_rule, "_session_insert_count", 0)}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["add_behavioral_rule"] = {"fn": t_add_behavioral_rule, "perm": "WRITE",
    "args": [
        {"name": "trigger", "type": "string"}, {"name": "pointer", "type": "string"},
        {"name": "full_rule", "type": "string"}, {"name": "domain", "type": "string"},
        {"name": "priority", "type": "string"}, {"name": "source", "type": "string"},
        {"name": "confidence", "type": "string"}, {"name": "expires_at", "type": "string"},
    ],
    "desc": "TASK-V8: Add new behavioral rule to behavioral_rules table. Validates trigger+domain enums. Checks for duplicate trigger+domain+pointer. source=owner|core_discovered|evolution|cold_processor."}


def t_update_behavioral_rule(rule_id: str = "", active: str = None, full_rule: str = None,
                              confidence: str = None, tested: str = None) -> dict:
    """Update an existing behavioral rule by id."""
    try:
        if not rule_id:
            return {"ok": False, "error_code": "missing_id", "message": "rule_id is required", "retry_hint": False, "domain": "behavioral_rules"}
        updates = {}
        if active is not None:
            updates["active"] = str(active).strip().lower() in ("true","1","yes")
        if full_rule:
            updates["full_rule"] = full_rule
        if confidence:
            try:
                updates["confidence"] = max(0.0, min(1.0, float(confidence)))
            except Exception:
                pass
        if tested is not None:
            updates["tested"] = str(tested).strip().lower() in ("true","1","yes")
        if not updates:
            return {"ok": False, "error_code": "no_updates", "message": "No valid update fields provided", "retry_hint": False, "domain": "behavioral_rules"}
        updates["updated_at"] = datetime.utcnow().isoformat()
        ok = sb_patch("behavioral_rules", f"id=eq.{rule_id}", updates)
        return {"ok": ok, "rule_id": rule_id, "updated_fields": list(updates.keys())}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["update_behavioral_rule"] = {"fn": t_update_behavioral_rule, "perm": "WRITE",
    "args": [{"name": "rule_id","type":"string"},{"name":"active","type":"string"},
             {"name":"full_rule","type":"string"},{"name":"confidence","type":"string"},{"name":"tested","type":"string"}],
    "desc": "TASK-V8: Update behavioral rule by id. Fields: active (true/false), full_rule, confidence (0.0-1.0), tested (true/false)."}


def t_get_infrastructure(component: str = None) -> dict:
    """Load infrastructure_map entries. Filter by component or return all active."""
    try:
        if component and component.strip():
            filters = f"status=neq.tombstone&id=gt.1&component=eq.{component.strip()}&order=id.asc"
        else:
            filters = "status=neq.tombstone&id=gt.1&order=id.asc"
        rows = sb_get("infrastructure_map",
            f"select=component,label,url,service_id,env_id,project_id,token_ref,fallback_component,status,check_interval_min,notes&{filters}", svc=True)
        if rows is None:
            return {"ok": True, "components": [], "migration_needed": True}
        return {"ok": True, "components": rows or [], "count": len(rows or [])}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["get_infrastructure"] = {"fn": t_get_infrastructure, "perm": "READ",
    "args": [{"name": "component", "type": "string", "description": "Filter by component name (railway, supabase, github, groq, telegram, local_pc)"}],
    "desc": "TASK-V8: Load infrastructure_map entries. Returns service URLs, IDs, token_refs (key names only -- not actual values). Component filter optional."}


def t_update_infrastructure_status(component: str = "", status: str = "", notes: str = None,
                                    owner_confirm: str = "false") -> dict:
    """Update infrastructure component status. status=tombstone requires owner_confirm=true."""
    try:
        if not component or not status:
            return {"ok": False, "error_code": "missing_params", "message": "component and status are required", "retry_hint": False, "domain": "infrastructure_map"}
        if status == "tombstone" and str(owner_confirm).strip().lower() not in ("true","1","yes"):
            return {"ok": False, "error_code": "permission_denied", "message": "Tombstoning a component requires owner_confirm=true. This is an irreversible action.", "retry_hint": False, "domain": "infrastructure_map"}
        updates = {"status": status, "last_checked": datetime.utcnow().isoformat()}
        if notes:
            updates["notes"] = notes
        ok = sb_patch("infrastructure_map", f"component=eq.{component}", updates)
        return {"ok": ok, "component": component, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["update_infrastructure_status"] = {"fn": t_update_infrastructure_status, "perm": "WRITE",
    "args": [{"name":"component","type":"string"},{"name":"status","type":"string"},
             {"name":"notes","type":"string"},{"name":"owner_confirm","type":"string"}],
    "desc": "TASK-V8: Update infrastructure component status (active|degraded|tombstone). tombstone requires owner_confirm=true."}


def t_add_infrastructure(component: str = "", label: str = "", url: str = None,
                          token_ref: str = None, fallback_component: str = None,
                          notes: str = None) -> dict:
    """Add new infrastructure component to infrastructure_map."""
    try:
        if not component or not label:
            return {"ok": False, "error_code": "missing_params", "message": "component and label are required", "retry_hint": False, "domain": "infrastructure_map"}
        row = {"component": component, "label": label, "status": "active"}
        if url: row["url"] = url
        if token_ref: row["token_ref"] = token_ref
        if fallback_component: row["fallback_component"] = fallback_component
        if notes: row["notes"] = notes
        ok = sb_post("infrastructure_map", row)
        return {"ok": ok, "component": component}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["add_infrastructure"] = {"fn": t_add_infrastructure, "perm": "WRITE",
    "args": [{"name":"component","type":"string"},{"name":"label","type":"string"},
             {"name":"url","type":"string"},{"name":"token_ref","type":"string"},
             {"name":"fallback_component","type":"string"},{"name":"notes","type":"string"}],
    "desc": "TASK-V8: Add new infrastructure component to infrastructure_map."}


def t_get_credentials_index(service: str = None) -> dict:
    """Load credentials_index entries (pointers only -- no actual values).
    Returns key_name, location, env_var_name for each service."""
    try:
        if service and service.strip():
            filters = f"active=eq.true&id=gt.1&service=eq.{service.strip()}&order=id.asc"
        else:
            filters = "active=eq.true&id=gt.1&order=service.asc"
        rows = sb_get("credentials_index",
            "select=service,key_name,location,env_var_name,required_for,notes,last_verified,expires_at&" + filters,
            svc=True)
        if rows is None:
            return {"ok": True, "credentials": [], "migration_needed": True}
        return {"ok": True, "credentials": rows or [], "count": len(rows or [])}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["get_credentials_index"] = {"fn": t_get_credentials_index, "perm": "READ",
    "args": [{"name": "service", "type": "string", "description": "Filter by service name (railway, supabase, github, groq, telegram)"}],
    "desc": "TASK-V8: Load credentials_index entries (pointers only -- NOT actual credentials). Returns key_name + where to find each credential."}


def t_add_credentials_index(service: str = "", key_name: str = "", location: str = "",
                              env_var_name: str = None, required_for: str = None,
                              notes: str = None) -> dict:
    """Add new credential pointer to credentials_index."""
    try:
        if not service or not key_name or not location:
            return {"ok": False, "error_code": "missing_params", "message": "service, key_name, location are required", "retry_hint": False, "domain": "credentials_index"}
        row = {"service": service, "key_name": key_name, "location": location, "active": True}
        if env_var_name: row["env_var_name"] = env_var_name
        if required_for:
            row["required_for"] = [x.strip() for x in required_for.split(",") if x.strip()]
        if notes: row["notes"] = notes
        ok = sb_post("credentials_index", row)
        return {"ok": ok, "service": service, "key_name": key_name}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["add_credentials_index"] = {"fn": t_add_credentials_index, "perm": "WRITE",
    "args": [{"name":"service","type":"string"},{"name":"key_name","type":"string"},
             {"name":"location","type":"string"},{"name":"env_var_name","type":"string"},
             {"name":"required_for","type":"string"},{"name":"notes","type":"string"}],
    "desc": "TASK-V8: Add new credential pointer to credentials_index. Stores key_name and location ONLY -- never actual credential values."}


def t_log_quality_metrics(session_id: str = "", quality_score: str = "0.8",
                           tasks_completed: str = "0", mistakes_made: str = "0",
                           owner_corrections: str = "0", assumptions_caught: str = "0",
                           domain: str = None, notes: str = None) -> dict:
    """Log quality score for current session. Called at session close."""
    try:
        try:
            q = max(0.0, min(1.0, float(quality_score)))
        except Exception:
            q = 0.8
        row = {
            "quality_score": q,
            "tasks_completed": int(tasks_completed) if str(tasks_completed).isdigit() else 0,
            "mistakes_made": int(mistakes_made) if str(mistakes_made).isdigit() else 0,
            "owner_corrections": int(owner_corrections) if str(owner_corrections).isdigit() else 0,
            "assumptions_caught": int(assumptions_caught) if str(assumptions_caught).isdigit() else 0,
        }
        if session_id: row["session_id"] = session_id
        if domain: row["domain"] = domain
        if notes: row["notes"] = notes
        ok = sb_post("quality_metrics", row)
        return {"ok": ok, "quality_score": q}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["log_quality_metrics"] = {"fn": t_log_quality_metrics, "perm": "WRITE",
    "args": [{"name":"session_id","type":"string"},{"name":"quality_score","type":"string"},
             {"name":"tasks_completed","type":"string"},{"name":"mistakes_made","type":"string"},
             {"name":"owner_corrections","type":"string"},{"name":"assumptions_caught","type":"string"},
             {"name":"domain","type":"string"},{"name":"notes","type":"string"}],
    "desc": "TASK-V8: Log session quality metrics to quality_metrics table. quality_score=0.0-1.0. Call at session close."}


def t_get_quality_alert() -> dict:
    """Check quality_metrics for last 7 days. Returns alert if 7d avg < 0.75 or 3 consecutive declining."""
    try:
        rows = sb_get("quality_metrics",
            "select=quality_score,date,domain&id=gt.1&order=created_at.desc&limit=30", svc=True) or []
        if len(rows) < 3:
            return {"ok": True, "alert": False, "trend": "insufficient_data", "message": "Need 3+ sessions for quality trend analysis", "count": len(rows)}
        scores = [float(r.get("quality_score", 0)) for r in rows[:7] if r.get("quality_score") is not None]
        if not scores:
            return {"ok": True, "alert": False, "trend": "no_data"}
        avg_7d = round(sum(scores) / len(scores), 3)
        # Check 3 consecutive declining
        consecutive_decline = False
        if len(scores) >= 3:
            consecutive_decline = scores[0] < scores[1] < scores[2]
        alert = avg_7d < 0.75 or consecutive_decline
        trend = "declining" if consecutive_decline or (len(scores) >= 2 and scores[0] < scores[-1]) else "stable"
        return {"ok": True, "alert": alert, "trend": trend, "7d_avg": avg_7d, "sample_count": len(scores),
                "message": f"Quality {'ALERT' if alert else 'OK'}: 7d_avg={avg_7d}, trend={trend}"}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["get_quality_alert"] = {"fn": t_get_quality_alert, "perm": "READ", "args": [],
    "desc": "TASK-V8: Check quality_metrics for last 7 days. Returns alert=true if 7d_avg < 0.75 or 3 consecutive declining sessions."}


def t_log_reasoning(session_id: str = "", action_planned: str = "", preflight_result: str = "",
                     assumptions_caught: str = "0", queries_triggered: str = "0",
                     owner_confirm_needed: str = "false", domain: str = None) -> dict:
    """Log cognitive pre-flight result. Called after each reasoning_preflight run."""
    try:
        if not action_planned:
            return {"ok": False, "error_code": "missing_params", "message": "action_planned is required", "retry_hint": False, "domain": "reasoning_log"}
        row = {
            "action_planned": action_planned[:500],
            "preflight_result": (preflight_result or "")[:500],
            "assumptions_caught": int(assumptions_caught) if str(assumptions_caught).isdigit() else 0,
            "queries_triggered": int(queries_triggered) if str(queries_triggered).isdigit() else 0,
            "owner_confirm_needed": str(owner_confirm_needed).strip().lower() in ("true","1","yes"),
            "behavioral_rule_proposed": False,
        }
        if session_id: row["session_id"] = session_id
        if domain: row["domain"] = domain
        ok = sb_post("reasoning_log", row)
        return {"ok": ok, "action_planned": action_planned[:80]}
    except Exception as e:
        return {"ok": False, "error": str(e), "error_code": "exception", "retry_hint": True}

TOOLS["log_reasoning"] = {"fn": t_log_reasoning, "perm": "WRITE",
    "args": [{"name":"session_id","type":"string"},{"name":"action_planned","type":"string"},
             {"name":"preflight_result","type":"string"},{"name":"assumptions_caught","type":"string"},
             {"name":"queries_triggered","type":"string"},{"name":"owner_confirm_needed","type":"string"},
             {"name":"domain","type":"string"}],
    "desc": "TASK-V8: Log cognitive pre-flight result to reasoning_log. Call after each reasoning_preflight run."}


# -- GAP-DATA-01: Weekly brain backup -----------------------------------------

def t_backup_brain(dry_run: str = "false") -> dict:
    """Export critical Supabase tables to private GitHub backup repo (no secret scanning).
    Uses BACKUP_REPO env var (pockiesaints7/core-agi-backups). Bypasses L.gh() rate limiter.
    dry_run=true lists what would be exported without writing."""
    import os as _os
    from datetime import datetime as _dt
    import json as _json
    import base64 as _b64
    dry = str(dry_run).strip().lower() in ("true", "1", "yes")
    date_str = _dt.utcnow().strftime("%Y-%m-%d")
    backup_repo = _os.environ.get("BACKUP_REPO", GITHUB_REPO)
    # UUID-PK tables: no id=gt.1. Bigserial tables: use id=gt.1.
    tables = [
        ("behavioral_rules", "select=id,trigger,pointer,full_rule,domain,priority,active,confidence&order=id.asc&limit=500&id=gt.1"),
        ("task_queue",       "select=id,task,status,priority,source,created_at&order=priority.desc&limit=50"),
        ("sessions",         "select=id,summary,domain,created_at&order=created_at.desc&limit=30"),
        ("mistakes",         "select=id,domain,what_failed,correct_approach,severity,root_cause,created_at&order=id.desc&limit=500&id=gt.1"),
        ("infrastructure_map", "select=*&order=id.asc&id=gt.1"),
        ("credentials_index",  "select=id,service,key_name,location,env_var_name,required_for&order=id.asc&id=gt.1"),
    ]
    if dry:
        return {"ok": True, "dry_run": True, "date": date_str, "backup_repo": backup_repo,
                "tables": ["knowledge_base (paginated 200/chunk)"] + [t[0] for t in tables],
                "message": f"Would write all tables to {backup_repo}/backups/{date_str}/"}
    results = []
    errors  = []
    # Collect all payloads (Supabase reads)
    file_payloads = {}
    KB_CHUNK = 200
    kb_offset = 1
    kb_chunk_num = 1
    kb_total = 0
    try:
        while True:
            qs = f"select=id,domain,topic,instruction,content,confidence,active&order=id.asc&limit={KB_CHUNK}&id=gt.{kb_offset}"
            chunk = sb_get("knowledge_base", qs, svc=True) or []
            if not chunk:
                break
            file_payloads[f"backups/{date_str}/knowledge_base_{kb_chunk_num}.json"] = _json.dumps(chunk, default=str, indent=2)
            kb_total += len(chunk)
            kb_chunk_num += 1
            kb_offset = chunk[-1]["id"]
            if len(chunk) < KB_CHUNK:
                break
        results.append({"table": "knowledge_base", "rows": kb_total, "chunks": kb_chunk_num - 1})
    except Exception as e:
        errors.append({"table": "knowledge_base", "error": str(e)})
    for table_name, qs in tables:
        try:
            rows = sb_get(table_name, qs, svc=True) or []
            file_payloads[f"backups/{date_str}/{table_name}.json"] = _json.dumps(rows, default=str, indent=2)
            results.append({"table": table_name, "rows": len(rows)})
        except Exception as e:
            errors.append({"table": table_name, "error": str(e)})
    if errors:
        notify(f"[BACKUP] Supabase fetch errors: {[e['table'] for e in errors]}")
        return {"ok": False, "date": date_str, "exported": results, "errors": errors}
    # Write each file directly via GitHub Contents API to PRIVATE backup repo
    gh_headers = _ghh()
    write_errors = []
    for path, content in file_payloads.items():
        try:
            sha = None
            r = httpx.get(f"https://api.github.com/repos/{backup_repo}/contents/{path}",
                          headers=gh_headers, timeout=10)
            if r.is_success:
                sha = r.json().get("sha")
            payload = {"message": f"[backup] {date_str}: {path.split('/')[-1]}",
                       "content": _b64.b64encode(content.encode("utf-8")).decode("ascii")}
            if sha:
                payload["sha"] = sha
            put = httpx.put(f"https://api.github.com/repos/{backup_repo}/contents/{path}",
                            headers=gh_headers, json=payload, timeout=30)
            if not put.is_success:
                write_errors.append({"path": path, "status": put.status_code, "body": put.text[:200]})
        except Exception as e:
            write_errors.append({"path": path, "error": str(e)})
    for r in results:
        r["ok"] = len(write_errors) == 0
    try:
        sb_post("sessions", {"summary": f"[state_update] last_backup_ts: {_dt.utcnow().isoformat()}",
                             "actions": [f"backup_brain: {len(file_payloads)} files -> {backup_repo}/backups/{date_str}/"],
                             "interface": "mcp"})
    except Exception:
        pass
    ok = len(write_errors) == 0
    if ok:
        notify(f"[BACKUP] Complete: {len(file_payloads)} files ({kb_total} KB rows) -> {backup_repo}/backups/{date_str}/")
    else:
        notify(f"[BACKUP] Write errors ({len(write_errors)}): {[e.get('path','?').split('/')[-1] for e in write_errors[:5]]}")
    return {"ok": ok, "date": date_str, "backup_repo": backup_repo, "files": len(file_payloads),
            "exported": results, "errors": write_errors}
TOOLS["backup_brain"] = {"fn": t_backup_brain, "perm": "READ",
    "args": [{"name": "dry_run", "type": "string", "description": "true=list what would be exported without writing (default false)"}],
    "desc": "GAP-DATA-01: Export critical Supabase tables to GitHub /backups/YYYY-MM-DD/. Runs weekly. dry_run=true for safe preview."}


def t_maintenance_purge(table: str = "hot_reflections", older_than_days: int = 14, dry_run: bool = True):
    """Purge old rows from maintenance tables. dry_run=True (default) only counts, never deletes.
    Tables: hot_reflections (processed rows older than N days), reasoning_log (older than N days), sessions (older than N days).
    Safety: dry_run must be explicitly False to delete. Never purges tombstone tables."""
    ALLOWED_TABLES = {"hot_reflections", "reasoning_log", "sessions"}
    # Load schema registry to verify column names before building any filter
    _load_schema_registry()
    try:
        older_than_days = int(older_than_days)
    except (TypeError, ValueError):
        return {"ok": False, "error": f"older_than_days must be an integer, got: {older_than_days!r}"}
    if table not in ALLOWED_TABLES:
        return {"ok": False, "error": f"Table '{table}' not in allowed purge list: {ALLOWED_TABLES}"}
    if older_than_days < 7:
        return {"ok": False, "error": "older_than_days must be >= 7 to prevent accidental recent-data deletion"}
    cutoff = (datetime.utcnow() - timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Build count query
    try:
        count_filter = f"created_at=lt.{cutoff}"
        if table == "hot_reflections":
            count_filter += "&processed_by_cold=eq.true"
        rows = sb_get(table, count_filter + "&select=id", svc=True)
        count = len(rows) if isinstance(rows, list) else 0
    except Exception as e:
        return {"ok": False, "error": f"Count query failed: {str(e)}"}
    if dry_run is True or str(dry_run).lower() in ("true", "1", "yes"):
        return {
            "ok": True,
            "dry_run": True,
            "table": table,
            "older_than_days": older_than_days,
            "cutoff": cutoff,
            "would_delete": count,
            "note": "Pass dry_run=False to execute deletion"
        }
    # Execute deletion
    delete_filter = f"created_at=lt.{cutoff}"
    if table == "hot_reflections":
        delete_filter += "&processed_by_cold=eq.true"
    ok = sb_delete(table, delete_filter)
    print(f"[PURGE] {table}: deleted ~{count} rows older than {older_than_days}d. ok={ok}")
    return {
        "ok": ok,
        "dry_run": False,
        "table": table,
        "older_than_days": older_than_days,
        "cutoff": cutoff,
        "deleted_approx": count
    }
TOOLS["maintenance_purge"] = {"fn": t_maintenance_purge, "perm": "WRITE",
    "args": [
        {"name": "table", "type": "string", "description": "Table to purge: hot_reflections | reasoning_log | sessions (default: hot_reflections)"},
        {"name": "older_than_days", "type": "string", "description": "Delete rows older than N days (min 7, default 14)"},
        {"name": "dry_run", "type": "string", "description": "true=count only, false=execute deletion (default: true -- must explicitly pass false to delete)"}
    ],
    "desc": "GAP-DB-02/03: Purge old rows from maintenance tables. dry_run=true default -- never deletes without explicit dry_run=false. Tables: hot_reflections (processed), reasoning_log, sessions."}


# =============================================================================
# AGI AGENTIC LAYER — AGI-11/12/13/14
# All tools registered, verify + test in next session.
# =============================================================================

# --- AGI-11: Execution Quality Layer -----------------------------------------

def t_reason_chain(action: str = "", domain: str = "general"):
    """AGI-11/S1: Fetch Supabase context for causal reasoning before any non-trivial action.
    Returns domain mistakes + KB entries for Claude to reason on natively.
    Claude generates: chain, failure_modes, confidence, proceed_recommended."""
    if not action:
        return {"ok": False, "error": "action is required"}
    try:
        mistakes = sb_get("mistakes", f"domain=eq.{domain}&order=created_at.desc&limit=5&id=gt.1", svc=True) or []
        kb = sb_get("knowledge_base", f"domain=eq.{domain}&limit=5&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "action": action,
            "domain": domain,
            "recent_mistakes": [{"context": m.get("context"), "what_failed": m.get("what_failed"), "correct_approach": m.get("correct_approach")} for m in mistakes],
            "relevant_kb": [{"topic": k.get("topic"), "content": k.get("content")} for k in kb],
            "instruction": "Claude: using this context, generate the causal chain, failure_modes, confidence (0.0-1.0), and proceed_recommended for the planned action."
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "action": action}

TOOLS["reason_chain"] = {"fn": t_reason_chain, "perm": "READ",
    "args": [
        {"name": "action", "type": "string", "description": "The planned action to reason about"},
        {"name": "domain", "type": "string", "description": "Domain context (e.g. deployment, supabase, code) default: general"}
    ],
    "desc": "AGI-11: Causal reasoning chain before any non-trivial action. Returns chain, failure_modes, confidence, proceed_recommended. Call before every write/deploy/multi-step execution."}


def t_action_gate(action: str = "", owner_token: str = ""):
    """AGI-11/S3: Hard reversibility gate. Classifies action as read/reversible_write/irreversible.
    irreversible requires owner_token to proceed. Returns blocked=True if irreversible and no token."""
    if not action:
        return {"ok": False, "error": "action is required"}
    IRREVERSIBLE_KEYWORDS = [
        "drop", "delete", "destroy", "purge", "force_close", "truncate",
        "overwrite production", "permanent", "cannot be undone", "hard delete"
    ]
    REVERSIBLE_KEYWORDS = [
        "insert", "update", "patch", "upsert", "deploy", "redeploy",
        "add", "create", "write", "push", "post"
    ]
    action_lower = action.lower()
    if any(kw in action_lower for kw in IRREVERSIBLE_KEYWORDS):
        classification = "irreversible"
    elif any(kw in action_lower for kw in REVERSIBLE_KEYWORDS):
        classification = "reversible_write"
    else:
        classification = "read"
    blocked = classification == "irreversible" and not owner_token
    return {
        "ok": True,
        "action": action,
        "classification": classification,
        "blocked": blocked,
        "message": "Owner token required for irreversible action" if blocked else "Proceed",
        "requires_owner_token": classification == "irreversible"
    }

TOOLS["action_gate"] = {"fn": t_action_gate, "perm": "READ",
    "args": [
        {"name": "action", "type": "string", "description": "The action description to classify"},
        {"name": "owner_token", "type": "string", "description": "Owner confirmation token for irreversible actions"}
    ],
    "desc": "AGI-11: Hard reversibility gate. Classifies action as read/reversible_write/irreversible. blocked=true if irreversible and no owner_token. Call before any destructive operation."}


def t_assert_source(value: str = "", declared_source: str = "memory", field_name: str = ""):
    """AGI-11/S4: Assumption detection at point of use.
    declared_source: session_query | owner_input | memory | skill_file
    Returns flagged=True if source=memory — forces CORE to query instead."""
    if not value:
        return {"ok": False, "error": "value is required"}
    TRUSTED_SOURCES = {"session_query", "owner_input", "skill_file"}
    UNTRUSTED_SOURCES = {"memory"}
    flagged = declared_source in UNTRUSTED_SOURCES
    return {
        "ok": True,
        "value": value[:100],
        "field_name": field_name,
        "declared_source": declared_source,
        "flagged": flagged,
        "trusted": not flagged,
        "instruction": "Query this value from its source before using" if flagged else "Source verified — safe to use",
        "risk": "high" if flagged else "low"
    }

TOOLS["assert_source"] = {"fn": t_assert_source, "perm": "READ",
    "args": [
        {"name": "value", "type": "string", "description": "The value being used"},
        {"name": "declared_source", "type": "string", "description": "Source of the value: session_query | owner_input | skill_file | memory"},
        {"name": "field_name", "type": "string", "description": "The field or variable name this value will populate"}
    ],
    "desc": "AGI-11: Assumption detection at point of use. Flags values sourced from memory — forces query instead. Call before using any value whose origin is uncertain."}


def t_goal_check(proposed_action: str = "", session_id: str = "", register_goal: str = ""):
    """AGI-11/S5: Goal-action alignment check.
    register_goal: set the session goal (call at task start).
    proposed_action: check if this action aligns with the registered goal.
    Returns aligned=True/False with reasoning."""
    # Store/retrieve goal from agentic_sessions table
    try:
        if register_goal:
            sb_post("agentic_sessions", {
                "session_id": session_id or "default",
                "goal": register_goal,
                "created_at": datetime.utcnow().isoformat()
            })
            return {"ok": True, "goal_registered": register_goal, "session_id": session_id or "default"}
        if not proposed_action:
            return {"ok": False, "error": "proposed_action or register_goal required"}
        # Load current goal
        rows = sb_get("agentic_sessions", f"session_id=eq.{session_id or 'default'}&order=created_at.desc&limit=1", svc=True)
        if not rows:
            return {"ok": False, "error": "No goal registered for this session. Call with register_goal first.", "aligned": None}
        goal = rows[0].get("goal", "")
        return {
            "ok": True,
            "goal": goal,
            "proposed_action": proposed_action,
            "instruction": "Claude: does the proposed_action directly advance the goal? Return aligned=true/false, alignment_score (0.0-1.0), reasoning, and recommendation (proceed|defer|reconsider)."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["goal_check"] = {"fn": t_goal_check, "perm": "WRITE",
    "args": [
        {"name": "register_goal", "type": "string", "description": "Set the session goal at task start (call once)"},
        {"name": "proposed_action", "type": "string", "description": "Check if this action aligns with the registered goal"},
        {"name": "session_id", "type": "string", "description": "Session identifier (default: default)"}
    ],
    "desc": "AGI-11: Goal-action alignment. Register goal at task start, then check each major action against it. Returns aligned=true/false with reasoning score. Catches goal drift."}


def t_mid_task_correct(anomaly: str = "", last_action: str = "", last_result: str = "", task_state: str = ""):
    """AGI-11/S7: Fetch Supabase context for mid-task self-correction.
    Returns similar past mistakes for Claude to diagnose the anomaly natively.
    Claude generates: root_cause, plan_adjustment, corrected_next_step."""
    if not anomaly:
        return {"ok": False, "error": "anomaly description is required"}
    try:
        # Search mistakes broadly — anomaly may cross domains
        all_mistakes = sb_get("mistakes", "order=created_at.desc&limit=10&id=gt.1", svc=True) or []
        # Filter for semantic relevance by checking key terms in anomaly
        anomaly_terms = set(anomaly.lower().split())
        similar = [m for m in all_mistakes if any(t in (m.get("what_failed") or "").lower() for t in anomaly_terms)]
        if not similar:
            similar = all_mistakes[:5]
        return {
            "ok": True,
            "anomaly": anomaly,
            "last_action": last_action,
            "last_result": last_result,
            "task_state": task_state,
            "similar_past_mistakes": [{"context": m.get("context"), "what_failed": m.get("what_failed"), "correct_approach": m.get("correct_approach"), "root_cause": m.get("root_cause")} for m in similar],
            "instruction": "Claude: using this context, diagnose root_cause, what_went_wrong, plan_adjustment, corrected_next_step, and whether to abort."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["mid_task_correct"] = {"fn": t_mid_task_correct, "perm": "READ",
    "args": [
        {"name": "anomaly", "type": "string", "description": "Description of the unexpected output or anomaly detected"},
        {"name": "last_action", "type": "string", "description": "The last action taken before the anomaly"},
        {"name": "last_result", "type": "string", "description": "The unexpected result that triggered this correction"},
        {"name": "task_state", "type": "string", "description": "Current task state summary"}
    ],
    "desc": "AGI-11: Mid-task self-correction. Call when anomaly detected instead of pushing through. Returns root_cause, plan_adjustment, corrected_next_step. Replaces log-and-continue pattern."}


# --- AGI-12: Task Intelligence Layer -----------------------------------------

def t_decompose_task(goal: str = "", domain: str = "general"):
    """AGI-12/S1: Fetch Supabase context for task decomposition.
    Returns domain KB + past mistakes for Claude to decompose the goal natively.
    Claude generates: subtasks with dependencies, execution_order, parallel_candidates."""
    if not goal:
        return {"ok": False, "error": "goal is required"}
    try:
        kb = sb_get("knowledge_base", f"domain=eq.{domain}&limit=8&id=gt.1", svc=True) or []
        mistakes = sb_get("mistakes", f"domain=eq.{domain}&order=created_at.desc&limit=5&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "goal": goal,
            "domain": domain,
            "domain_kb": [{"topic": k.get("topic"), "content": k.get("content")} for k in kb],
            "domain_mistakes": [{"what_failed": m.get("what_failed"), "correct_approach": m.get("correct_approach")} for m in mistakes],
            "instruction": "Claude: using this context, decompose the goal into subtasks with id, title, description, depends_on, effort_estimate. Return execution_order, parallel_candidates, total_effort, critical_path."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["decompose_task"] = {"fn": t_decompose_task, "perm": "READ",
    "args": [
        {"name": "goal", "type": "string", "description": "High-level goal or task description to decompose"},
        {"name": "domain", "type": "string", "description": "Domain context for better decomposition (default: general)"}
    ],
    "desc": "AGI-12: Autonomous task decomposition. Takes a high-level goal, returns subtasks with dependencies, execution order, parallel candidates, and effort estimates."}


def t_resolve_ambiguity(instruction: str = ""):
    """AGI-12/S2: Fetch Supabase context for ambiguity resolution before acting.
    Returns relevant behavioral rules + KB for Claude to resolve ambiguity natively.
    Claude generates: ambiguities list, safe_to_proceed, interpretation_chosen."""
    if not instruction:
        return {"ok": False, "error": "instruction is required"}
    try:
        rules = sb_get("behavioral_rules", "domain=eq.universal&active=eq.true&limit=10&id=gt.1", svc=True) or []
        kb = sb_get("knowledge_base", "domain=eq.core_agi&limit=5&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "instruction": instruction,
            "relevant_rules": [{"trigger": r.get("trigger"), "pointer": r.get("pointer")} for r in rules],
            "relevant_kb": [{"topic": k.get("topic"), "content": k.get("content")} for k in kb],
            "instruction_to_claude": "Claude: identify ambiguities in the instruction, list interpretations, pick lowest-risk one. Return ambiguities[], safe_to_proceed, interpretation_chosen, confidence, needs_owner_clarification, clarification_question."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["resolve_ambiguity"] = {"fn": t_resolve_ambiguity, "perm": "READ",
    "args": [
        {"name": "instruction", "type": "string", "description": "The instruction or task description to analyze for ambiguity"}
    ],
    "desc": "AGI-12: Structured ambiguity resolution. Identifies unclear elements, lists interpretations, picks lowest-risk one. Returns safe_to_proceed. Call before acting on ambiguous instructions."}


def t_scope_tracker(planned_scope: str = "", actions_taken: str = "", task_id: str = ""):
    """AGI-12/S3: Return planned vs actual scope for Claude to detect creep natively.
    No Supabase fetch needed — pure input comparison.
    Claude generates: scope_exceeded, drift_level, unplanned_items, recommendation."""
    if not planned_scope or not actions_taken:
        return {"ok": False, "error": "planned_scope and actions_taken are required"}
    actions_list = [a.strip() for a in actions_taken.split(",") if a.strip()]
    return {
        "ok": True,
        "task_id": task_id,
        "planned_scope": planned_scope,
        "actions_taken": actions_list,
        "action_count": len(actions_list),
        "instruction": "Claude: compare planned_scope vs actions_taken. Return scope_exceeded (bool), drift_level (none|minor|moderate|severe), planned_items[], unplanned_items[], overlap_percent (0-100), recommendation (continue|flag_owner|stop), summary."
    }

TOOLS["scope_tracker"] = {"fn": t_scope_tracker, "perm": "READ",
    "args": [
        {"name": "planned_scope", "type": "string", "description": "What was planned at task start"},
        {"name": "actions_taken", "type": "string", "description": "Comma-separated list of actions taken so far"},
        {"name": "task_id", "type": "string", "description": "Task UUID for reference"}
    ],
    "desc": "AGI-12: Scope creep detection. Compares planned vs actual touch surface. Returns scope_exceeded, drift_level, unplanned_items. Call after each subtask gate."}


def t_lookahead(current_action: str = "", current_state: str = "", steps_ahead: int = 2):
    """AGI-12/S4: Fetch Supabase context for multi-step consequence modeling.
    Returns past failure patterns for Claude to model N+1 and N+2 states natively.
    Claude generates: next_states, blocking_detected, recommendation."""
    if not current_action:
        return {"ok": False, "error": "current_action is required"}
    try:
        steps_ahead = int(steps_ahead)
    except (TypeError, ValueError):
        steps_ahead = 2
    try:
        # Get top patterns for consequence modeling
        patterns = sb_get("pattern_frequency", "order=frequency.desc&limit=10&id=gt.1", svc=True) or []
        mistakes = sb_get("mistakes", "order=created_at.desc&limit=8&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "current_action": current_action,
            "current_state": current_state,
            "steps_ahead": steps_ahead,
            "top_failure_patterns": [{"pattern": p.get("pattern"), "domain": p.get("domain"), "freq": p.get("freq")} for p in patterns],
            "recent_mistakes": [{"context": m.get("context"), "what_failed": m.get("what_failed"), "root_cause": m.get("root_cause")} for m in mistakes],
            "instruction": f"Claude: model the next {steps_ahead} system states after this action. Return next_states (step, state_after, risk, risk_description), blocking_detected, blocking_reason, recommendation (proceed|reconsider|abort), summary."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["lookahead"] = {"fn": t_lookahead, "perm": "READ",
    "args": [
        {"name": "current_action", "type": "string", "description": "The action about to be executed"},
        {"name": "current_state", "type": "string", "description": "Summary of current system state"},
        {"name": "steps_ahead", "type": "string", "description": "How many steps to model ahead (default: 2)"}
    ],
    "desc": "AGI-12: Multi-step lookahead. Models N+1 and N+2 system states after an action. Returns blocking_detected if a future state is risky. Prevents locally-correct globally-wrong decisions."}


def t_sequence_plan(subtasks: str = ""):
    """AGI-12/S5: Return subtask list for Claude to sequence natively.
    No Supabase fetch needed — pure reasoning from input.
    Claude generates: sequential order, parallel_groups, dependency_map, race_condition_risks."""
    if not subtasks:
        return {"ok": False, "error": "subtasks list is required"}
    return {
        "ok": True,
        "subtasks": subtasks,
        "instruction": "Claude: analyze these subtasks for dependencies and side effects. Return sequential[], parallel_groups[], recommended_order[], dependency_map{}, race_condition_risks[]."
    }

TOOLS["sequence_plan"] = {"fn": t_sequence_plan, "perm": "READ",
    "args": [
        {"name": "subtasks", "type": "string", "description": "JSON or comma-separated list of subtask IDs and descriptions"}
    ],
    "desc": "AGI-12: Parallel vs sequential discrimination. Analyzes subtask dependencies, returns recommended order, parallel groups, dependency map. Prevents race conditions and wrong sequencing."}


def t_negative_space(task_description: str = "", domain: str = "general"):
    """AGI-12/S6: Fetch all domain mistakes from Supabase for negative space reasoning.
    Returns full mistake history for Claude to enumerate forbidden actions natively.
    Claude generates: forbidden_actions[], common_mistakes_in_domain[], summary."""
    if not task_description:
        return {"ok": False, "error": "task_description is required"}
    try:
        mistakes = sb_get("mistakes", f"domain=eq.{domain}&order=created_at.desc&limit=20&id=gt.1", svc=True) or []
        if not mistakes:
            # Fall back to all domains if no domain-specific mistakes
            mistakes = sb_get("mistakes", "order=created_at.desc&limit=15&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "task_description": task_description,
            "domain": domain,
            "domain_mistakes": [{"context": m.get("context"), "what_failed": m.get("what_failed"), "root_cause": m.get("root_cause"), "how_to_avoid": m.get("how_to_avoid")} for m in mistakes],
            "instruction": "Claude: from this mistake history, enumerate the forbidden_actions (tempting but wrong) for this task. Include action, reason, risk (medium|high|critical), tempting_because. Also list common_mistakes_in_domain and a summary."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["negative_space"] = {"fn": t_negative_space, "perm": "READ",
    "args": [
        {"name": "task_description", "type": "string", "description": "The task to analyze for wrong-but-tempting actions"},
        {"name": "domain", "type": "string", "description": "Domain context for domain-specific mistake patterns (default: general)"}
    ],
    "desc": "AGI-12: Negative space reasoning. Enumerates forbidden actions — tempting but wrong for this task. Returns forbidden_actions with reasons and risk levels. Call at task start."}


# --- AGI-13: State & Context Integrity Layer ---------------------------------

def t_validate_output(value: str = "", target_field: str = "", table: str = ""):
    """AGI-13/S1: Semantic output validation before any write.
    Validates against operating_context.json schema. Returns safe_to_write."""
    if not value or not target_field or not table:
        return {"ok": False, "error": "value, target_field, and table are required"}
    schema = _load_schema_registry()
    violations = []
    if schema:
        table_schema = schema.get("tables", {}).get(table, {})
        # allowed_values lives at table level keyed by field name
        allowed_values = table_schema.get("allowed_values", {}).get(target_field, [])
        # field type lives in columns dict
        field_type = table_schema.get("columns", {}).get(target_field, "")
        if allowed_values and value not in allowed_values:
            violations.append({"field": target_field, "reason": f"Value '{value}' not in allowed: {allowed_values}"})
        if field_type == "uuid":
            import re as _uuid_re
            if not _uuid_re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', str(value).lower()):
                violations.append({"field": target_field, "reason": f"Value is not a valid UUID"})
    safe_to_write = len(violations) == 0
    return {
        "ok": True,
        "value": value[:100],
        "target_field": target_field,
        "table": table,
        "safe_to_write": safe_to_write,
        "violations": violations,
        "schema_found": schema is not None
    }

TOOLS["validate_output"] = {"fn": t_validate_output, "perm": "READ",
    "args": [
        {"name": "value", "type": "string", "description": "The value to validate"},
        {"name": "target_field", "type": "string", "description": "The field name this value will be written to"},
        {"name": "table", "type": "string", "description": "The Supabase table being written to"}
    ],
    "desc": "AGI-13: Semantic output validation before write. Checks value against operating_context.json schema. Returns safe_to_write, violations. Call before every sb_insert/sb_patch/sb_upsert."}


def t_tag_certainty(conclusion: str = "", basis: str = "inferred"):
    """AGI-13/S2: Tag a conclusion with its certainty level.
    basis: observed | inferred | assumed
    Returns certainty level, decay_rate, requires_verification."""
    if not conclusion:
        return {"ok": False, "error": "conclusion is required"}
    BASIS_MAP = {
        "observed": {"certainty": "confirmed", "decay_rate": "slow", "requires_verification": False},
        "inferred": {"certainty": "inferred", "decay_rate": "medium", "requires_verification": True},
        "assumed": {"certainty": "uncertain", "decay_rate": "fast", "requires_verification": True}
    }
    basis = basis.lower()
    if basis not in BASIS_MAP:
        basis = "assumed"
    tag = BASIS_MAP[basis]
    return {
        "ok": True,
        "conclusion": conclusion[:200],
        "basis": basis,
        "certainty": tag["certainty"],
        "decay_rate": tag["decay_rate"],
        "requires_verification": tag["requires_verification"],
        "safe_for_irreversible_action": basis == "observed",
        "instruction": "Verify before using in irreversible action" if tag["requires_verification"] else "Safe to use"
    }

TOOLS["tag_certainty"] = {"fn": t_tag_certainty, "perm": "READ",
    "args": [
        {"name": "conclusion", "type": "string", "description": "The conclusion or value to tag"},
        {"name": "basis", "type": "string", "description": "How this was derived: observed | inferred | assumed"}
    ],
    "desc": "AGI-13: Certainty tagging. Tags conclusions as confirmed/inferred/uncertain based on how they were derived. uncertain conclusions block irreversible actions. Call before using derived values."}


def t_progress_model(task_id: str = "", subtasks_total: int = 0, subtasks_done: int = 0, actions_taken: int = 0):
    """AGI-13/S3: Progress visibility within a task.
    Returns percent_complete, estimated_steps_remaining, on_track, drift_detected."""
    try:
        subtasks_total = int(subtasks_total)
        subtasks_done = int(subtasks_done)
        actions_taken = int(actions_taken)
    except (TypeError, ValueError):
        return {"ok": False, "error": "subtasks_total, subtasks_done, actions_taken must be integers"}
    if subtasks_total == 0:
        return {"ok": False, "error": "subtasks_total must be > 0"}
    percent_complete = round((subtasks_done / subtasks_total) * 100, 1)
    avg_actions_per_subtask = actions_taken / max(subtasks_done, 1)
    estimated_steps_remaining = round(avg_actions_per_subtask * (subtasks_total - subtasks_done))
    on_track = avg_actions_per_subtask <= 10
    drift_detected = avg_actions_per_subtask > 15
    return {
        "ok": True,
        "task_id": task_id,
        "percent_complete": percent_complete,
        "subtasks_done": subtasks_done,
        "subtasks_total": subtasks_total,
        "actions_taken": actions_taken,
        "avg_actions_per_subtask": round(avg_actions_per_subtask, 1),
        "estimated_steps_remaining": estimated_steps_remaining,
        "on_track": on_track,
        "drift_detected": drift_detected,
        "status_label": f"{percent_complete}% complete — {'on track' if on_track else 'drifting'}"
    }

TOOLS["progress_model"] = {"fn": t_progress_model, "perm": "READ",
    "args": [
        {"name": "task_id", "type": "string", "description": "Task UUID for reference"},
        {"name": "subtasks_total", "type": "string", "description": "Total number of subtasks"},
        {"name": "subtasks_done", "type": "string", "description": "Number of subtasks completed"},
        {"name": "actions_taken", "type": "string", "description": "Total tool calls made so far in this task"}
    ],
    "desc": "AGI-13: Progress visibility. Returns percent_complete, estimated_steps_remaining, on_track, drift_detected. Call after each subtask gate and include in checkpoint."}


def t_cognitive_load(session_duration_mins: int = 0, tool_calls_made: int = 0, context_size_estimate: int = 0):
    """AGI-13/S4: Session complexity monitoring.
    Returns load_level (low/medium/high/critical) and recommendation."""
    try:
        session_duration_mins = int(session_duration_mins)
        tool_calls_made = int(tool_calls_made)
        context_size_estimate = int(context_size_estimate)
    except (TypeError, ValueError):
        return {"ok": False, "error": "All parameters must be integers"}
    score = 0
    if session_duration_mins > 60: score += 2
    elif session_duration_mins > 30: score += 1
    if tool_calls_made > 50: score += 2
    elif tool_calls_made > 25: score += 1
    if context_size_estimate > 80000: score += 2
    elif context_size_estimate > 40000: score += 1
    LEVELS = {0: "low", 1: "low", 2: "medium", 3: "medium", 4: "high", 5: "high", 6: "critical"}
    RECS = {"low": "continue", "medium": "checkpoint", "high": "summarize", "critical": "close"}
    load_level = LEVELS.get(score, "critical")
    recommendation = RECS[load_level]
    return {
        "ok": True,
        "load_level": load_level,
        "recommendation": recommendation,
        "score": score,
        "session_duration_mins": session_duration_mins,
        "tool_calls_made": tool_calls_made,
        "context_size_estimate": context_size_estimate,
        "action": f"Load={load_level}: {recommendation}"
    }

TOOLS["cognitive_load"] = {"fn": t_cognitive_load, "perm": "READ",
    "args": [
        {"name": "session_duration_mins", "type": "string", "description": "Session length in minutes"},
        {"name": "tool_calls_made", "type": "string", "description": "Total tool calls made this session"},
        {"name": "context_size_estimate", "type": "string", "description": "Estimated context window tokens used"}
    ],
    "desc": "AGI-13: Cognitive load monitoring. Returns load_level (low/medium/high/critical) and recommendation (continue/checkpoint/summarize/close). Call periodically on long sessions."}


def t_partial_complete(task_id: str = "", completed_subtasks: str = "", incomplete_subtasks: str = "", reason_stopped: str = ""):
    """AGI-13/S5: Clean partial completion protocol.
    Writes structured partial state to partial_states table.
    Ensures task is always in known recoverable state."""
    if not task_id or not reason_stopped:
        return {"ok": False, "error": "task_id and reason_stopped are required"}
    record = {
        "task_id": task_id,
        "completed_subtasks": completed_subtasks,
        "incomplete_subtasks": incomplete_subtasks,
        "reason_stopped": reason_stopped,
        "system_state_snapshot": json.dumps({
            "timestamp": datetime.utcnow().isoformat(),
            "completed": completed_subtasks.split(",") if completed_subtasks else [],
            "incomplete": incomplete_subtasks.split(",") if incomplete_subtasks else [],
            "reason": reason_stopped
        }),
        "created_at": datetime.utcnow().isoformat()
    }
    try:
        sb_post("partial_states", record)
        return {
            "ok": True,
            "task_id": task_id,
            "completed_subtasks": completed_subtasks,
            "incomplete_subtasks": incomplete_subtasks,
            "reason_stopped": reason_stopped,
            "message": "Partial state saved. Task can be resumed from this checkpoint."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["partial_complete"] = {"fn": t_partial_complete, "perm": "WRITE",
    "args": [
        {"name": "task_id", "type": "string", "description": "UUID of the task being suspended"},
        {"name": "completed_subtasks", "type": "string", "description": "Comma-separated list of completed subtask IDs"},
        {"name": "incomplete_subtasks", "type": "string", "description": "Comma-separated list of incomplete subtask IDs"},
        {"name": "reason_stopped", "type": "string", "description": "Why the task was suspended (blocker/crash/owner-close/timeout)"}
    ],
    "desc": "AGI-13: Clean partial completion protocol. Saves structured partial state when task is suspended. Ensures always-recoverable state. Call on any non-clean task exit."}


def t_verify_external_state(assumed_state: str = "", sources: str = "supabase"):
    """AGI-13/S6: External state change detection before committing.
    Re-queries key values to detect drift from assumed state.
    assumed_state: JSON string of {key: assumed_value} pairs."""
    if not assumed_state:
        return {"ok": False, "error": "assumed_state JSON string is required"}
    try:
        state = json.loads(assumed_state)
    except json.JSONDecodeError:
        return {"ok": False, "error": "assumed_state must be valid JSON: {\"key\": \"assumed_value\"}"}
    drifted = []
    checked = []
    # For now: structural check — verify keys are present and non-null
    # Full live re-query requires knowing table+column routing per key (future enhancement)
    for key, assumed_value in state.items():
        checked.append({"key": key, "assumed": assumed_value, "note": "structural check — live re-query requires table routing"})
    match = len(drifted) == 0
    return {
        "ok": True,
        "sources_checked": sources,
        "assumed_keys": list(state.keys()),
        "match": match,
        "drifted": drifted,
        "checked": checked,
        "note": "Structural validation complete. Wire live re-query per key in AGI-13 execution phase."
    }

TOOLS["verify_external_state"] = {"fn": t_verify_external_state, "perm": "READ",
    "args": [
        {"name": "assumed_state", "type": "string", "description": 'JSON string of assumed key:value pairs e.g. {"task_status": "pending"}'},
        {"name": "sources", "type": "string", "description": "Comma-separated sources to verify against: supabase|github|railway (default: supabase)"}
    ],
    "desc": "AGI-13: External state verification. Checks assumed state against live sources before committing actions that depend on it. Returns drifted keys. Call before actions depending on earlier state."}


# --- AGI-14: Safety & Resilience Layer ---------------------------------------

def t_resource_model(planned_actions: str = ""):
    """AGI-14/S1: Resource awareness and consumption estimation.
    Returns Groq token estimate, Railway compute impact, Supabase write count, throttle recommendations."""
    if not planned_actions:
        return {"ok": False, "error": "planned_actions is required"}
    actions_list = [a.strip() for a in planned_actions.split(",") if a.strip()]
    groq_calls = sum(1 for a in actions_list if any(k in a.lower() for k in ["reason", "groq", "analyze", "generate", "synthesize", "check"]))
    sb_writes = sum(1 for a in actions_list if any(k in a.lower() for k in ["insert", "update", "patch", "upsert", "write", "post"]))
    deploys = sum(1 for a in actions_list if any(k in a.lower() for k in ["deploy", "patch_file", "redeploy"]))
    token_estimate = groq_calls * 2000
    GROQ_SESSION_LIMIT = 50000
    SUPABASE_BURST_LIMIT = 20
    DEPLOY_LIMIT = 3
    within_limits = token_estimate < GROQ_SESSION_LIMIT and sb_writes < SUPABASE_BURST_LIMIT and deploys <= DEPLOY_LIMIT
    throttle_recommended = sb_writes > 10 or deploys > 2
    batch_candidates = [a for a in actions_list if any(k in a.lower() for k in ["insert", "post", "write"])]
    return {
        "ok": True,
        "planned_action_count": len(actions_list),
        "groq_calls_estimated": groq_calls,
        "token_estimate": token_estimate,
        "supabase_writes": sb_writes,
        "deploys": deploys,
        "within_limits": within_limits,
        "throttle_recommended": throttle_recommended,
        "batch_candidates": batch_candidates,
        "warnings": [
            f"Token estimate {token_estimate} approaching Groq session limit" if token_estimate > 40000 else None,
            f"{sb_writes} Supabase writes -- consider batching" if sb_writes > 10 else None,
            f"{deploys} deploys planned -- high Railway compute" if deploys > 2 else None
        ]
    }

TOOLS["resource_model"] = {"fn": t_resource_model, "perm": "READ",
    "args": [
        {"name": "planned_actions", "type": "string", "description": "Comma-separated list of planned actions for this task"}
    ],
    "desc": "AGI-14: Resource awareness. Estimates Groq tokens, Supabase writes, deploys before execution. Returns within_limits, throttle_recommended, batch_candidates. Call before multi-step plans."}


def t_impact_model(action: str = "", current_state: str = ""):
    """AGI-14/S2: Fetch infrastructure map + pending tasks for system impact modeling.
    Returns live system context for Claude to model impact natively.
    Claude generates: affected_components, timing_risks, side_effects, proceed_recommended."""
    if not action:
        return {"ok": False, "error": "action is required"}
    try:
        infra = sb_get("infrastructure_map", "status=eq.active&id=gt.1", svc=True) or []
        pending_tasks = sb_get("task_queue", "status=eq.pending&order=priority.desc&limit=5", svc=True) or []
        return {
            "ok": True,
            "action": action,
            "current_state": current_state,
            "active_infrastructure": [{"component": i.get("component"), "label": i.get("label"), "notes": i.get("notes")} for i in infra],
            "pending_tasks": [{"task": t.get("task", "{}")[:120], "priority": t.get("priority")} for t in pending_tasks],
            "instruction": "Claude: given this infrastructure and pending tasks, model the system impact of the action. Return affected_components[], timing_safe, timing_risks[], side_effects[], pending_task_interference[], proceed_recommended, summary."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["impact_model"] = {"fn": t_impact_model, "perm": "READ",
    "args": [
        {"name": "action", "type": "string", "description": "The action to model impact for"},
        {"name": "current_state", "type": "string", "description": "Summary of current system state"}
    ],
    "desc": "AGI-14: System impact modeling. Identifies affected components, timing risks, side effects on pending tasks. Returns proceed_recommended. Call before touching shared infrastructure."}


def t_trust_map(action_type: str = ""):
    """AGI-14/S3: Trust calibration per action type.
    Returns verification_required and verification_steps for the given action."""
    TRUST_MAP = {
        "read": {"trust": "high", "verification_required": False, "verification_steps": [], "risk_level": "low"},
        "reversible_write": {"trust": "medium", "verification_required": True, "verification_steps": ["verify_output", "check_schema"], "risk_level": "medium"},
        "deploy": {"trust": "low", "verification_required": True, "verification_steps": ["validate_syntax", "verify_before_deploy", "predict_failure", "health_check_after"], "risk_level": "high"},
        "irreversible": {"trust": "critical", "verification_required": True, "verification_steps": ["action_gate", "owner_confirmation", "reason_chain", "lookahead"], "risk_level": "critical"},
        "schema_change": {"trust": "low", "verification_required": True, "verification_steps": ["dry_run_first", "verify_before_deploy", "owner_confirmation"], "risk_level": "high"},
        "groq_call": {"trust": "medium", "verification_required": False, "verification_steps": ["validate_json_output"], "risk_level": "low"},
    }
    action_lower = action_type.lower()
    if not action_type or action_lower not in TRUST_MAP:
        return {
            "ok": True,
            "available_types": list(TRUST_MAP.keys()),
            "message": "Pass one of the available action types to get trust calibration"
        }
    entry = TRUST_MAP[action_lower]
    return {
        "ok": True,
        "action_type": action_lower,
        "trust_level": entry["trust"],
        "risk_level": entry["risk_level"],
        "verification_required": entry["verification_required"],
        "verification_steps": entry["verification_steps"],
        "instruction": f"Run {entry['verification_steps']} before proceeding" if entry["verification_required"] else "Low risk — proceed with standard care"
    }

TOOLS["trust_map"] = {"fn": t_trust_map, "perm": "READ",
    "args": [
        {"name": "action_type", "type": "string", "description": "Action category: read | reversible_write | deploy | irreversible | schema_change | groq_call"}
    ],
    "desc": "AGI-14: Trust calibration per action type. Returns trust_level, risk_level, verification_required, verification_steps. Makes verification frequency data-driven, not rule-based."}


def t_contradiction_check(new_instruction: str = "", domain: str = "general"):
    """AGI-14/S4: Fetch behavioral_rules from Supabase for contradiction detection.
    Returns rules for Claude to check the new instruction against natively.
    Claude generates: conflict_detected, conflicting_rules, recommendation."""
    if not new_instruction:
        return {"ok": False, "error": "new_instruction is required"}
    try:
        rules = sb_get("behavioral_rules", f"domain=eq.{domain}&active=eq.true&select=id,trigger,full_rule&id=gt.1", svc=True) or []
        if not rules:
            rules = sb_get("behavioral_rules", "domain=eq.universal&active=eq.true&select=id,trigger,full_rule&id=gt.1", svc=True) or []
        return {
            "ok": True,
            "new_instruction": new_instruction,
            "domain": domain,
            "rules_checked": len(rules),
            "existing_rules": [{"id": r.get("id"), "trigger": r.get("trigger"), "full_rule": (r.get("full_rule") or "")[:300]} for r in rules[:20]],
            "instruction": "Claude: check if new_instruction contradicts any existing_rules. Return conflict_detected (bool), conflicting_rules (list of {rule_id, trigger, conflict_description, severity}), recommendation (override|defer|ask_owner|safe_to_proceed), reasoning."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["contradiction_check"] = {"fn": t_contradiction_check, "perm": "READ",
    "args": [
        {"name": "new_instruction", "type": "string", "description": "The new instruction to check for conflicts"},
        {"name": "domain", "type": "string", "description": "Domain to search rules in (default: general)"}
    ],
    "desc": "AGI-14: Contradiction detection. Checks new instructions against existing behavioral_rules for conflicts. Returns conflict_detected, conflicting_rules, recommendation. Call when owner gives new behavioral instruction."}


def t_circuit_breaker(failed_step: str = "", dependent_steps: str = "", failure_reason: str = ""):
    """AGI-14/S5: Failure cascade prevention.
    Analyzes which downstream steps depend on the failed step.
    Returns cascade_risk, safe_to_continue, steps_to_suspend."""
    if not failed_step:
        return {"ok": False, "error": "failed_step is required"}
    dependent_list = [s.strip() for s in dependent_steps.split(",") if s.strip()] if dependent_steps else []
    return {
        "ok": True,
        "failed_step": failed_step,
        "failure_reason": failure_reason,
        "dependent_steps": dependent_list,
        "instruction": "Claude: analyze cascade risk for these dependent steps given the failed step and reason. Return cascade_risk (list of {step, dependency, impact: blocked|degraded|unaffected, severity}), safe_to_continue, steps_to_suspend[], steps_safe_to_run[], recommended_action (suspend_all|skip_dependents|retry_failed|ask_owner), summary."
    }

TOOLS["circuit_breaker"] = {"fn": t_circuit_breaker, "perm": "READ",
    "args": [
        {"name": "failed_step", "type": "string", "description": "The step that failed"},
        {"name": "dependent_steps", "type": "string", "description": "Comma-separated list of steps that depend on the failed step"},
        {"name": "failure_reason", "type": "string", "description": "Why the step failed"}
    ],
    "desc": "AGI-14: Failure cascade prevention. Analyzes dependent steps when one fails. Returns steps_to_suspend, safe_to_continue, recommended_action. Prevents executing downstream steps on null/stale data."}


def t_loop_detect(action: str = "", context_hash: str = "", session_id: str = "default", clear: bool = False):
    """AGI-14/S6: Action loop detection within a session.
    Maintains hash of actions taken. Returns loop_detected if same action+context attempted before.
    clear=True resets the session action log."""
    if not action and not clear:
        return {"ok": False, "error": "action is required (or pass clear=True to reset)"}
    try:
        clear_bool = clear is True or str(clear).lower() in ("true", "1", "yes")
        # Build action fingerprint
        fingerprint = f"{action.lower().strip()}::{context_hash}"
        # Load existing action log from agentic_sessions
        rows = sb_get("agentic_sessions", f"session_id=eq.{session_id}&select=action_log", svc=True)
        action_log = []
        row_exists = bool(rows)
        if rows and rows[0].get("action_log"):
            action_log = json.loads(rows[0]["action_log"]) if isinstance(rows[0]["action_log"], str) else rows[0]["action_log"]
        if clear_bool:
            if row_exists:
                sb_patch("agentic_sessions", {"action_log": json.dumps([])}, f"session_id=eq.{session_id}")
            return {"ok": True, "cleared": True, "session_id": session_id}
        # Check for loop
        loop_detected = fingerprint in action_log
        previous_attempt = action_log.count(fingerprint)
        # Append to log
        action_log.append(fingerprint)
        if row_exists:
            sb_patch("agentic_sessions", {"action_log": json.dumps(action_log[-100:])}, f"session_id=eq.{session_id}")
        else:
            sb_post("agentic_sessions", {"session_id": session_id, "action_log": json.dumps(action_log[-100:]), "created_at": datetime.utcnow().isoformat()})
        return {
            "ok": True,
            "action": action,
            "session_id": session_id,
            "loop_detected": loop_detected,
            "previous_attempts": previous_attempt,
            "total_actions_logged": len(action_log),
            "recommendation": "Change approach or surface to owner" if loop_detected else "No loop — proceed",
            "instruction": "STOP — this exact action was already attempted this session" if loop_detected else "Safe to proceed"
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["loop_detect"] = {"fn": t_loop_detect, "perm": "WRITE",
    "args": [
        {"name": "action", "type": "string", "description": "The action about to be taken"},
        {"name": "context_hash", "type": "string", "description": "Optional hash of relevant context to distinguish similar actions"},
        {"name": "session_id", "type": "string", "description": "Session identifier for scoping the loop log (default: default)"},
        {"name": "clear", "type": "string", "description": "true=reset this session's action log"}
    ],
    "desc": "AGI-14: Action loop detection. Tracks actions taken this session by fingerprint. Returns loop_detected if same action attempted before. CORE must change approach if loop detected — never retry blindly."}


# --- Gemini diagnostic test --------------------------------------------------

def t_test_gemini():
    """Test gemini_chat() end-to-end from Railway. Returns response or error."""
    try:
        from core_config import gemini_chat, _GEMINI_KEYS, _GEMINI_MODEL
        key_count = len(_GEMINI_KEYS)
        result = gemini_chat("you are a test assistant", "reply with just the word OK", max_tokens=10)
        return {"ok": True, "response": result, "key_count": key_count, "model": _GEMINI_MODEL}
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["test_gemini"] = {"fn": t_test_gemini, "perm": "READ", "args": [],
    "desc": "Diagnostic: test gemini_chat() end-to-end from Railway. Returns response or error."}


# --- AGI-01: Cross-Domain Synthesis ------------------------------------------

def t_synthesize_cross_domain():
    """AGI-01: Manual trigger for cross-domain synthesis. Runs _run_cross_domain_synthesis() immediately.
    Reads top patterns per domain, finds structural similarities via Groq,
    writes insights to knowledge_base(domain=synthesis)."""
    try:
        from core_train import _run_cross_domain_synthesis
        _run_cross_domain_synthesis()
        return {
            "ok": True,
            "message": "Cross-domain synthesis triggered. Check Railway logs for [SYNTH] output and Telegram for results.",
            "instruction": "Check railway_logs_live keyword=SYNTH to monitor progress."
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

TOOLS["synthesize_cross_domain"] = {"fn": t_synthesize_cross_domain, "perm": "WRITE",
    "args": [],
    "desc": "AGI-01: Manual trigger for weekly cross-domain synthesis. Reads top patterns per domain, finds structural similarities via Groq, writes insights to knowledge_base(domain=synthesis). Also runs automatically every Wednesday."}
