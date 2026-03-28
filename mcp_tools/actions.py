"""
Jarvis Actions Router v1.3
===========================
NEW in v1.3 (2026-03-08):
  POST /actions/context       — Brain-first context engine. Call before every response.
                                Takes message keywords → returns unified multi-table context:
                                playbook methods + mistake warnings + KB entries + memory + growth flags.
  POST /actions/session_state — Rich cross-session continuity. Structured task state save/load.
                                Replaces thin summary string for complex multi-session tasks.

EXISTING endpoints unchanged from v1.2.
"""
from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional, List, Any, Dict
from modules.db import sql, esc, dq
import os, json, base64, zipfile, io, re
from datetime import datetime, timezone

router = APIRouter(tags=["actions"])

SECRET = os.environ.get("JARVIS_SECRET", "changeme")
H = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


# ═══════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ═══════════════════════════════════════════════════════════

async def _brain_boot() -> dict:
    tables = {
        "memory":         "SELECT * FROM memory ORDER BY category, key",
        "knowledge_base": "SELECT id, domain, topic, tags, confidence FROM knowledge_base ORDER BY id",
        "mistakes":       "SELECT id, context, what_failed, root_cause, correct_approach, tags FROM mistakes ORDER BY id",
        "playbook":       "SELECT topic, method, why_best, supersedes, previous_method, version, tags FROM playbook ORDER BY topic",
        "sessions":       "SELECT summary, actions, interface, created_at FROM sessions ORDER BY created_at DESC LIMIT 20",
    }
    results = {}
    for name, query in tables.items():
        try:
            results[name] = await sql(query)
        except Exception as e:
            results[name] = {"error": str(e)}
    return results


async def _brain_compact() -> dict:
    results = {}
    try:
        counts_raw = await sql("""
            SELECT
                (SELECT COUNT(*) FROM memory) AS memory,
                (SELECT COUNT(*) FROM knowledge_base) AS knowledge_base,
                (SELECT COUNT(*) FROM mistakes) AS mistakes,
                (SELECT COUNT(*) FROM playbook) AS playbook,
                (SELECT COUNT(*) FROM sessions) AS sessions
        """)
        results["counts"] = counts_raw[0] if counts_raw else {}
    except Exception as e:
        results["counts"] = {"error": str(e)}

    try:
        last = await sql("SELECT summary, actions, interface, created_at FROM sessions ORDER BY created_at DESC LIMIT 1")
        results["last_session"] = last[0] if last else None
    except Exception as e:
        results["last_session"] = None

    try:
        mem = await sql("SELECT value FROM memory WHERE key = 'brain_health_report' LIMIT 1")
        results["scanner"] = json.loads(mem[0]["value"]) if mem else {}
    except Exception as e:
        results["scanner"] = {}

    try:
        pending = await sql("SELECT key, value FROM memory WHERE key LIKE 'pending_task_%'")
        active_pending = []
        for p in (pending or []):
            try:
                v = json.loads(p["value"])
                if v.get("status") != "complete":
                    active_pending.append({"key": p["key"], "value": v})
            except Exception:
                pass
        results["pending_tasks"] = active_pending
    except Exception as e:
        results["pending_tasks"] = []

    return results


async def _derive_absorbed_from_sessions() -> list:
    try:
        sessions = await sql("SELECT summary FROM sessions ORDER BY created_at DESC LIMIT 50")
        absorbed = set()
        for s in (sessions or []):
            matches = re.findall(r'data-[\w-]+\.zip|takeout-[\w-]+\.zip', s.get("summary", ""))
            absorbed.update(matches)
        return list(absorbed)
    except Exception:
        return []


def _health_report(brain: dict) -> dict:
    for m in (brain.get("memory") or []):
        if m.get("key") == "brain_health_report":
            try:
                return json.loads(m["value"])
            except Exception:
                pass
    return {}


def _tags_sql(tags: list) -> str:
    return "{" + ",".join(str(t) for t in tags) + "}"


# ═══════════════════════════════════════════════════════════
# 1. LIST
# ═══════════════════════════════════════════════════════════

@router.get("/list")
async def list_actions():
    return {
        "actions": [
            {
                "endpoint": "POST /actions/context",
                "purpose": "BRAIN-FIRST ENGINE. Call before every response. Takes keywords from Vuk's message → returns matching playbook methods (how to do it), mistake warnings (what to avoid), KB entries (background knowledge), memory keys, and active growth flags for those domains.",
                "token_saving": "Replaces ad-hoc /brain/search calls. One call surfaces everything relevant across all tables."
            },
            {
                "endpoint": "POST /actions/session_state/save",
                "purpose": "CROSS-SESSION CONTINUITY: Save rich structured task state. Fields: task_id, title, status, what_was_done[], decisions_made[], next_steps[], open_questions[], key_facts{str->str}, tools_used[], files_modified[], growth_flags_resolved[]",
                "token_saving": "Enables genuine task resumption instead of reconstruction from vague summaries."
            },
            {
                "endpoint": "POST /actions/session_state/load",
                "purpose": "CROSS-SESSION LOAD: Retrieve task state by task_id. Returns full struct. next_steps[0] = exactly where to resume.",
                "token_saving": "No need to reconstruct context from vague session summaries."
            },
            {
                "endpoint": "GET /actions/session_state/list",
                "purpose": "LIST active (non-complete) task states. Returns task_id, title, status, next_steps[:2], saved_at.",
                "token_saving": "Surfaced automatically in /actions/boot active_tasks field."
            },
            {
                "endpoint": "POST /actions/boot?compact=true",
                "purpose": "TOKEN-EFFICIENT boot: scanner+last_session+counts+unprocessed. No brain tables. Derives absorbed server-side. USE THIS by default.",
                "token_saving": "~80% fewer tokens than full boot."
            },
            {
                "endpoint": "POST /actions/boot",
                "purpose": "FULL boot: loads entire brain + scanner + unprocessed. Use only when brain data needed immediately.",
                "token_saving": "Replaces JARVIS-PROMPT Steps 3-6 (~6 tool calls -> 1)"
            },
            {
                "endpoint": "POST /actions/brain_write",
                "purpose": "Bulk write any combination: memory + KB + playbook + mistakes + session.",
                "token_saving": "Replaces N separate POST /brain/* calls -> 1 call"
            },
            {
                "endpoint": "POST /actions/session_end",
                "purpose": "End-of-session: saves all new learning then logs session.",
                "token_saving": "Replaces end-of-session loop (~5-10 calls -> 1)"
            },
            {
                "endpoint": "POST /actions/absorb_zip",
                "purpose": "Send base64 zip bytes, Railway parses conversations.json, saves insights.",
                "token_saving": "Replaces multi-step zip read + parse + save loop"
            },
            {
                "endpoint": "POST /actions/growth/reconcile",
                "purpose": "Act on a scanner growth flag.",
                "token_saving": "Replaces per-flag investigation + write loop"
            },
        ]
    }


# ═══════════════════════════════════════════════════════════
# 2. CONTEXT ENGINE — brain-first, fires before every response
# ═══════════════════════════════════════════════════════════

class ContextInput(BaseModel):
    keywords: List[str]                    # 1-5 domain keywords extracted from Vuk's message
    message_intent: str = ""               # optional: "task", "question", "fix", "deploy", etc.
    include_growth_flags: bool = True      # surface active scanner flags for these domains

@router.post("/context")
async def action_context(inp: ContextInput):
    """
    Brain-first context engine. Call this before responding to any message from Vuk.

    Given keywords extracted from the message, returns:
    - playbook: proven methods for those domains → use as HOW TO DO IT
    - mistakes: past failures in those domains → use as WARNINGS / SHIELDS
    - knowledge: KB entries for those domains → use as BACKGROUND CONTEXT
    - memory: relevant memory keys → use as PERSONAL FACTS
    - templates: code templates that apply → use BEFORE writing any boilerplate
    - growth_flags: active scanner flags for these domains → act on them inline
    - gaps: domains mentioned but with no KB coverage → flag as new knowledge opportunities

    One call replaces multiple ad-hoc /brain/search calls.
    """
    if not inp.keywords:
        return {"ok": False, "error": "keywords required"}

    results = {
        "ok": True,
        "keywords": inp.keywords,
        "playbook": [],
        "mistakes": [],
        "knowledge": [],
        "memory": [],
        "templates": [],
        "growth_flags": [],
        "gaps": [],
        "multi_hop": [],
    }

    # Build FTS query from keywords
    fts_terms = " | ".join(inp.keywords)  # OR search across all keywords
    keyword_pattern = "|".join(re.escape(k) for k in inp.keywords)

    # ── Playbook: find methods for these domains ──────────────
    try:
        pb_rows = await sql(f"""
            SELECT topic, method, why_best, tags, version
            FROM playbook
            WHERE topic ~* {dq(keyword_pattern)}
               OR method ~* {dq(keyword_pattern)}
               OR why_best ~* {dq(keyword_pattern)}
               OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ~* {dq(keyword_pattern)})
            ORDER BY version DESC
            LIMIT 5
        """)
        results["playbook"] = pb_rows or []
    except Exception as e:
        results["playbook"] = []

    # ── Mistakes: past failures as shields ────────────────────
    try:
        mk_rows = await sql(f"""
            SELECT id, context, what_failed, root_cause, correct_approach, tags
            FROM mistakes
            WHERE what_failed ~* {dq(keyword_pattern)}
               OR root_cause ~* {dq(keyword_pattern)}
               OR correct_approach ~* {dq(keyword_pattern)}
               OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ~* {dq(keyword_pattern)})
            ORDER BY id DESC
            LIMIT 8
        """)
        results["mistakes"] = mk_rows or []
    except Exception as e:
        results["mistakes"] = []

    # ── Knowledge Base: background context ────────────────────
    try:
        kb_rows = await sql(f"""
            SELECT id, domain, topic, content, tags, confidence
            FROM knowledge_base
            WHERE to_tsvector('english', topic || ' ' || content) @@ to_tsquery('english', {dq(fts_terms)})
               OR topic ~* {dq(keyword_pattern)}
               OR EXISTS (SELECT 1 FROM unnest(tags) t WHERE t ~* {dq(keyword_pattern)})
            ORDER BY confidence DESC, updated_at DESC
            LIMIT 6
        """)
        results["knowledge"] = kb_rows or []
    except Exception as e:
        results["knowledge"] = []

    # ── Memory: relevant personal/context facts ───────────────
    try:
        mem_rows = await sql(f"""
            SELECT category, key, value
            FROM memory
            WHERE key ~* {dq(keyword_pattern)}
               OR value ~* {dq(keyword_pattern)}
            ORDER BY updated_at DESC
            LIMIT 5
        """)
        results["memory"] = mem_rows or []
    except Exception as e:
        results["memory"] = []

    # ── Code Templates: what to use before writing boilerplate ─
    try:
        # Templates stored as memory keys: template_candidate_SLUG or known template list
        tmpl_rows = await sql(f"""
            SELECT key, value FROM memory
            WHERE (key LIKE 'template_%' OR category = 'templates')
              AND (key ~* {dq(keyword_pattern)} OR value ~* {dq(keyword_pattern)})
            LIMIT 5
        """)
        # Also check known template names against keywords
        known_templates = {
            "github": "github_push.ps1",
            "push": "github_push.ps1",
            "railway": "railway_deploy.ps1",
            "deploy": "railway_deploy.ps1",
            "zip": "zip_reader.ps1",
            "absorb": "zip_reader.ps1",
            "boot": "jarvis_boot.ps1",
            "wsl": "wsl_runner.ps1",
            "python": "wsl_runner.ps1",
            "supabase": "supabase_sql.ps1",
            "sql": "supabase_sql.ps1",
            "brain": "jarvis_api.ps1",
            "api": "jarvis_api.ps1",
            "session": "jarvis_api.ps1",
        }
        matched_templates = list(set(
            v for k, v in known_templates.items()
            if any(kw.lower() in k or k in kw.lower() for kw in inp.keywords)
        ))
        results["templates"] = matched_templates
    except Exception as e:
        results["templates"] = []

    # ── Growth Flags: active scanner flags for these domains ──
    if inp.include_growth_flags:
        try:
            report_row = await sql("SELECT value FROM memory WHERE key = 'brain_health_report' LIMIT 1")
            if report_row:
                report = json.loads(report_row[0]["value"])
                all_flags = report.get("growth_flags", []) + report.get("maintenance_flags", [])
                relevant_flags = []
                for flag in all_flags:
                    flag_text = json.dumps(flag).lower()
                    if any(kw.lower() in flag_text for kw in inp.keywords):
                        relevant_flags.append({
                            "type": flag.get("type"),
                            "severity": flag.get("severity"),
                            "message": flag.get("message", "")[:200],
                            "action": flag.get("action", "")[:200],
                        })
                results["growth_flags"] = relevant_flags
        except Exception:
            results["growth_flags"] = []

    # ── Multi-hop: KB entries that reference other KB entries ─
    # If KB results mention other topic slugs, surface those too
    try:
        if results["knowledge"]:
            referenced_topics = set()
            for kb in results["knowledge"]:
                content = kb.get("content", "")
                # Find references like "see topic_slug" or "→ topic_slug"
                refs = re.findall(r'(?:see|→|ref:|topic:)\s*([\w_]+)', content, re.I)
                referenced_topics.update(refs)
            if referenced_topics:
                ref_pattern = "|".join(re.escape(t) for t in list(referenced_topics)[:5])
                multi_hop = await sql(f"""
                    SELECT domain, topic, content, confidence
                    FROM knowledge_base
                    WHERE topic ~* {dq(ref_pattern)}
                    LIMIT 3
                """)
                results["multi_hop"] = multi_hop or []
    except Exception:
        results["multi_hop"] = []

    # ── Gaps: keywords with no coverage anywhere ──────────────
    gaps = []
    for kw in inp.keywords:
        has_kb = any(kw.lower() in str(r).lower() for r in results["knowledge"])
        has_pb = any(kw.lower() in str(r).lower() for r in results["playbook"])
        has_mk = any(kw.lower() in str(r).lower() for r in results["mistakes"])
        if not has_kb and not has_pb and not has_mk:
            gaps.append(kw)
    results["gaps"] = gaps

    # ── Summary ───────────────────────────────────────────────
    results["summary"] = {
        "playbook_hits": len(results["playbook"]),
        "mistake_warnings": len(results["mistakes"]),
        "kb_entries": len(results["knowledge"]),
        "memory_facts": len(results["memory"]),
        "templates_suggested": results["templates"],
        "growth_flags_active": len(results["growth_flags"]),
        "coverage_gaps": results["gaps"],
        "has_context": len(results["playbook"]) + len(results["mistakes"]) + len(results["knowledge"]) > 0,
    }

    return results


# ═══════════════════════════════════════════════════════════
# 3. SESSION STATE — rich cross-session continuity
# ═══════════════════════════════════════════════════════════

class SessionStateSave(BaseModel):
    task_id: str                           # unique slug, e.g. "boot_optimization_2026-03-08"
    title: str                             # human-readable task title
    status: str = "in_progress"            # "in_progress" | "blocked" | "complete"
    # What happened
    what_was_done: List[str] = []          # list of completed steps
    decisions_made: List[str] = []         # key decisions and why
    # What's next
    next_steps: List[str] = []             # ordered list of what to do next
    open_questions: List[str] = []         # unresolved questions blocking progress
    # Context needed to resume
    key_facts: Dict[str, str] = {}         # e.g. {"commit_sha": "abc123", "file_path": "..."}
    tools_used: List[str] = []             # which code templates / APIs / endpoints were used
    files_modified: List[str] = []         # files written or changed this session
    # Growth flags acted on (close them inline)
    growth_flags_resolved: List[str] = []

class SessionStateLoad(BaseModel):
    task_id: str

@router.post("/session_state/save")
async def save_session_state(inp: SessionStateSave):
    """
    Save rich structured task state for cross-session continuity.
    Replaces thin pending_task_* memory keys with fully structured resumable state.
    """
    state = {
        "task_id": inp.task_id,
        "title": inp.title,
        "status": inp.status,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "what_was_done": inp.what_was_done,
        "decisions_made": inp.decisions_made,
        "next_steps": inp.next_steps,
        "open_questions": inp.open_questions,
        "key_facts": inp.key_facts,
        "tools_used": inp.tools_used,
        "files_modified": inp.files_modified,
        "growth_flags_resolved": inp.growth_flags_resolved,
    }
    key = f"task_state_{inp.task_id}"
    try:
        q = f"INSERT INTO memory (category, key, value) VALUES ($$task_state$$, {dq(key)}, {dq(json.dumps(state))}) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"
        await sql(q)
        return {"ok": True, "key": key, "status": inp.status}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@router.post("/session_state/load")
async def load_session_state(inp: SessionStateLoad):
    """
    Load structured task state by task_id. Returns everything needed to resume.
    """
    key = f"task_state_{inp.task_id}"
    try:
        row = await sql(f"SELECT value, updated_at FROM memory WHERE key = {dq(key)} LIMIT 1")
        if not row:
            return {"ok": False, "error": f"No state found for task_id: {inp.task_id}"}
        state = json.loads(row[0]["value"])
        state["last_saved"] = row[0].get("updated_at")
        return {"ok": True, "state": state}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@router.get("/session_state/list")
async def list_session_states():
    """List all active (non-complete) task states."""
    try:
        rows = await sql("SELECT key, value, updated_at FROM memory WHERE category = 'task_state' ORDER BY updated_at DESC")
        states = []
        for r in (rows or []):
            try:
                v = json.loads(r["value"])
                states.append({
                    "task_id": v.get("task_id"),
                    "title": v.get("title"),
                    "status": v.get("status"),
                    "next_steps": v.get("next_steps", [])[:2],
                    "saved_at": v.get("saved_at"),
                })
            except Exception:
                pass
        return {"ok": True, "states": states}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# 4. BOOT
# ═══════════════════════════════════════════════════════════

class BootInput(BaseModel):
    interface: str = "claude-ai"
    export_files: List[str] = []
    absorbed_exports: List[str] = []
    skill_file_dates: Dict[str, str] = {}
    code_templates: List[str] = []

@router.post("/boot")
async def action_boot(inp: BootInput, compact: bool = Query(default=True)):
    try:
        all_exports = [f for f in inp.export_files if f != "README.md"]
        absorbed = inp.absorbed_exports
        if not absorbed and all_exports:
            absorbed = await _derive_absorbed_from_sessions()
        unprocessed = [f for f in all_exports if f not in absorbed]

        manifest_val = json.dumps({
            "last_boot": datetime.now(timezone.utc).isoformat(),
            "memory_exports": all_exports,
            "absorbed_exports": absorbed,
            "skill_files": inp.skill_file_dates,
            "code_templates": inp.code_templates,
        })
        await sql(f"INSERT INTO memory (category, key, value) VALUES ($$system$$, $$pc_manifest$$, {dq(manifest_val)}) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()")

        boot_summary = f"Boot via /actions/boot (compact={compact}). Interface: {inp.interface}. Unprocessed: {len(unprocessed)}."
        await sql(f"INSERT INTO sessions (summary, actions, interface) VALUES ({dq(boot_summary)}, '{{\"boot\",\"pc_manifest_push\"}}', {dq(inp.interface)})")

        # Always return active task states at boot
        try:
            task_rows = await sql("SELECT key, value FROM memory WHERE category = 'task_state' ORDER BY updated_at DESC LIMIT 5")
            active_tasks = []
            for r in (task_rows or []):
                try:
                    v = json.loads(r["value"])
                    if v.get("status") != "complete":
                        active_tasks.append({
                            "task_id": v.get("task_id"),
                            "title": v.get("title"),
                            "status": v.get("status"),
                            "next_steps": v.get("next_steps", [])[:3],
                            "open_questions": v.get("open_questions", []),
                        })
                except Exception:
                    pass
        except Exception:
            active_tasks = []

        if compact:
            data = await _brain_compact()
            return {
                "ok": True,
                "compact": True,
                "counts": data["counts"],
                "scanner": data["scanner"],
                "last_session": data["last_session"],
                "pending_tasks": data["pending_tasks"],
                "active_tasks": active_tasks,       # rich structured task states
                "unprocessed_exports": unprocessed,
            }
        else:
            brain = await _brain_boot()
            report = _health_report(brain)
            last_sessions = brain.get("sessions") or []
            return {
                "ok": True,
                "compact": False,
                "brain": brain,
                "scanner": report,
                "unprocessed_exports": unprocessed,
                "last_session": last_sessions[0] if last_sessions else None,
                "active_tasks": active_tasks,
                "counts": report.get("brain_counts", {}),
            }

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# 5. BULK BRAIN WRITE
# ═══════════════════════════════════════════════════════════

class BrainWriteInput(BaseModel):
    memory: List[Dict[str, str]] = []
    knowledge: List[Dict[str, Any]] = []
    playbook: List[Dict[str, Any]] = []
    mistakes: List[Dict[str, Any]] = []
    session: Optional[Dict[str, Any]] = None

@router.post("/brain_write")
async def action_brain_write(inp: BrainWriteInput):
    saved = {"memory": 0, "knowledge": 0, "playbook": 0, "mistakes": 0, "session": False}
    errors = []

    for m in inp.memory:
        try:
            q = f"INSERT INTO memory (category, key, value) VALUES ({dq(m['category'])}, {dq(m['key'])}, {dq(m['value'])}) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"
            await sql(q)
            saved["memory"] += 1
        except Exception as e:
            errors.append(f"memory/{m.get('key')}: {e}")

    for k in inp.knowledge:
        try:
            conf = k.get("confidence", "high")
            q = f"INSERT INTO knowledge_base (domain, topic, content, tags, confidence) VALUES ({dq(k['domain'])}, {dq(k['topic'])}, {dq(k['content'])}, '{_tags_sql(k.get('tags',[]))}', {dq(conf)}) ON CONFLICT (domain, topic) DO UPDATE SET content=EXCLUDED.content, tags=EXCLUDED.tags, confidence=EXCLUDED.confidence, updated_at=NOW()"
            await sql(q)
            saved["knowledge"] += 1
        except Exception as e:
            errors.append(f"knowledge/{k.get('topic')}: {e}")

    for p in inp.playbook:
        try:
            sup = dq(p.get("supersedes", ""))
            q = f"""INSERT INTO playbook (topic, method, why_best, supersedes, tags, version, previous_method)
VALUES ({dq(p['topic'])}, {dq(p['method'])}, {dq(p.get('why_best',''))}, {sup}, '{_tags_sql(p.get('tags',[]))}', 1, NULL)
ON CONFLICT (topic) DO UPDATE SET previous_method=playbook.method, method=EXCLUDED.method, why_best=EXCLUDED.why_best, supersedes=EXCLUDED.supersedes, tags=EXCLUDED.tags, version=playbook.version+1, updated_at=NOW()"""
            await sql(q)
            saved["playbook"] += 1
        except Exception as e:
            errors.append(f"playbook/{p.get('topic')}: {e}")

    for mk in inp.mistakes:
        try:
            rc = mk.get("root_cause") or mk.get("what_failed", "")
            q = f"INSERT INTO mistakes (context, what_failed, root_cause, correct_approach, tags) VALUES ({dq(mk['context'])}, {dq(mk['what_failed'])}, {dq(rc)}, {dq(mk.get('correct_approach',''))}, '{_tags_sql(mk.get('tags',[]))}')"
            await sql(q)
            saved["mistakes"] += 1
        except Exception as e:
            errors.append(f"mistake: {e}")

    if inp.session:
        try:
            s = inp.session
            iface = s.get("interface", "claude-ai")
            actions_sql = "{" + ",".join([f'"{esc(a)}"' for a in s.get("actions", [])]) + "}"
            q = f"INSERT INTO sessions (summary, actions, interface) VALUES ({dq(s['summary'])}, '{actions_sql}', {dq(iface)})"
            await sql(q)
            saved["session"] = True
        except Exception as e:
            errors.append(f"session: {e}")

    return {"ok": len(errors) == 0, "saved": saved, "errors": errors}


# ═══════════════════════════════════════════════════════════
# 6. SESSION END
# ═══════════════════════════════════════════════════════════

class SessionEndInput(BaseModel):
    summary: str
    actions: List[str] = []
    interface: str = "claude-ai"
    new_knowledge: List[Dict[str, Any]] = []
    new_playbook: List[Dict[str, Any]] = []
    new_mistakes: List[Dict[str, Any]] = []
    new_memory: List[Dict[str, str]] = []
    # Optional: auto-save session state if task is continuing
    task_state: Optional[Dict[str, Any]] = None

@router.post("/session_end")
async def action_session_end(inp: SessionEndInput):
    write_inp = BrainWriteInput(
        memory=inp.new_memory,
        knowledge=inp.new_knowledge,
        playbook=inp.new_playbook,
        mistakes=inp.new_mistakes,
        session={"summary": inp.summary, "actions": inp.actions, "interface": inp.interface}
    )
    result = await action_brain_write(write_inp)

    # If task_state provided, save it too
    if inp.task_state:
        try:
            state_inp = SessionStateSave(**inp.task_state)
            state_result = await save_session_state(state_inp)
            result["task_state_saved"] = state_result.get("ok", False)
        except Exception as e:
            result.setdefault("errors", []).append(f"task_state: {e}")

    return result


# ═══════════════════════════════════════════════════════════
# 7. ABSORB ZIP
# ═══════════════════════════════════════════════════════════

class AbsorbZipInput(BaseModel):
    filename: str
    content_b64: str
    interface: str = "claude-ai"

@router.post("/absorb_zip")
async def action_absorb_zip(inp: AbsorbZipInput):
    try:
        raw = base64.b64decode(inp.content_b64)
        zf = zipfile.ZipFile(io.BytesIO(raw))

        conv_file = next((n for n in zf.namelist() if "conversations" in n.lower() and n.endswith(".json")), None)
        if not conv_file:
            return {"ok": False, "error": f"No conversations.json found. Files: {zf.namelist()}"}

        conversations = json.loads(zf.read(conv_file).decode("utf-8"))
        brain = await _brain_boot()
        recent_sessions = " ".join(r.get("summary", "") for r in (brain.get("sessions") or []))

        saved = {"memory": 0, "knowledge": 0, "playbook": 0, "mistakes": 0}
        errors = []

        conv_list = conversations if isinstance(conversations, list) else []
        for conv in conv_list:
            title = conv.get("title", "untitled")
            messages = conv.get("messages", conv.get("mapping", {}))
            if isinstance(messages, dict):
                msg_list = [v.get("message", {}) for v in messages.values() if v.get("message")]
            else:
                msg_list = messages if isinstance(messages, list) else []

            for msg in (msg_list or []):
                if not msg:
                    continue
                role = msg.get("role", "")
                if not role:
                    author = msg.get("author", {})
                    role = author.get("role", "") if isinstance(author, dict) else ""
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
                if not content or role not in ("assistant", "user"):
                    continue

                if re.search(r'\b(4\d\d|5\d\d|error|failed|traceback)\b', content, re.I):
                    snippet = content[:80]
                    if snippet not in recent_sessions:
                        try:
                            ctx = f"Absorbed from {inp.filename}: {title}"
                            wf = content[:300]
                            ca = "Review original session for resolution"
                            q = f"INSERT INTO mistakes (context, what_failed, root_cause, correct_approach, tags) VALUES ({dq(ctx)}, {dq(wf)}, {dq('Extracted from memory export')}, {dq(ca)}, '{{\"absorption\",\"memory-export\"}}')"
                            await sql(q)
                            saved["mistakes"] += 1
                        except Exception as e:
                            errors.append(str(e))

        summary = f"Absorbed {inp.filename}: {saved['memory']} memory, {saved['knowledge']} KB, {saved['playbook']} playbook, {saved['mistakes']} mistakes."
        try:
            q = f"INSERT INTO sessions (summary, actions, interface) VALUES ({dq(summary)}, '{{\"zip_absorption\",\"memory_import\"}}', {dq(inp.interface)})"
            await sql(q)
        except Exception as e:
            errors.append(f"session: {e}")

        return {"ok": True, "filename": inp.filename, "saved": saved, "errors": errors, "summary": summary}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
# 8. GROWTH FLAG RECONCILE
# ═══════════════════════════════════════════════════════════

class GrowthActionInput(BaseModel):
    flag_type: str
    flag_data: Dict[str, Any] = {}

@router.post("/growth/reconcile")
async def action_growth_reconcile(inp: GrowthActionInput):
    flag_type = inp.flag_type
    data = inp.flag_data
    result = {}

    try:
        if flag_type == "proven_upgrade_candidates":
            topics = data.get("topics", [])
            upgraded = []
            for topic in topics:
                try:
                    await sql(f"UPDATE knowledge_base SET confidence='proven', updated_at=NOW() WHERE topic={dq(topic)}")
                    upgraded.append(topic)
                except Exception:
                    pass
            result = {"upgraded_to_proven": upgraded}

        elif flag_type == "knowledge_domain_gap":
            gap_domains = data.get("gap_domains", [])
            created = []
            for domain in gap_domains[:5]:
                mistakes = await sql(f"SELECT what_failed, correct_approach FROM mistakes WHERE {dq(domain)} = ANY(tags) LIMIT 5")
                if not mistakes:
                    continue
                lessons = "\n".join(f"- {m['what_failed']}: {m.get('correct_approach','')}" for m in mistakes)
                content = f"Domain: {domain}\nLessons from mistakes:\n{lessons}\n\nNote: Auto-generated stub. Expand with structured knowledge."
                topic_slug = domain + "_from_mistakes"
                try:
                    q = f"INSERT INTO knowledge_base (domain, topic, content, tags, confidence) VALUES ({dq(domain)}, {dq(topic_slug)}, {dq(content)}, '{{\"auto-generated\"}}', 'medium') ON CONFLICT (domain, topic) DO UPDATE SET content=EXCLUDED.content, updated_at=NOW()"
                    await sql(q)
                    created.append(topic_slug)
                except Exception:
                    pass
            result = {"kb_stubs_created": created}

        elif flag_type == "synthesis_opportunity":
            domain = data.get("domain", "")
            entries = await sql(f"SELECT topic, content FROM knowledge_base WHERE domain={dq(domain)}")
            if entries:
                synthesis = f"# {domain.title()} Master Guide\n\nAuto-synthesized from {len(entries)} entries.\n\n"
                for e in entries:
                    synthesis += f"## {e['topic']}\n{str(e['content'])[:500]}\n\n"
                topic_slug = domain + "_master_guide"
                try:
                    q = f"INSERT INTO knowledge_base (domain, topic, content, tags, confidence) VALUES ({dq(domain)}, {dq(topic_slug)}, {dq(synthesis)}, '{{\"master-guide\",\"synthesis\"}}', 'medium') ON CONFLICT (domain, topic) DO UPDATE SET content=EXCLUDED.content, updated_at=NOW()"
                    await sql(q)
                    result = {"master_guide_created": topic_slug, "sources": len(entries)}
                except Exception as e:
                    result = {"error": str(e)}
        else:
            result = {"note": f"Flag type '{flag_type}' requires manual Claude action."}

        return {"ok": True, "flag_type": flag_type, "result": result}

    except Exception as e:
        return {"ok": False, "error": str(e)}
