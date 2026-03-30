"""core_semantic_projection.py -- silent semantic projection for raw CORE tables.

This worker mirrors important raw brain data into knowledge_base as a semantic
read model. Raw tables remain canonical. The projection is best-effort,
idempotent, and intentionally silent: no Telegram notifications.

Goal:
- keep raw task/session/context tables unchanged
- automatically project new important inputs into knowledge_base
- let the native semantic auto-embed path handle the KB row itself
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any

from core_config import _env_int, sb_get, sb_upsert

PROJECTION_ENABLED = os.getenv("CORE_SEMANTIC_PROJECTION_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
PROJECTION_INTERVAL_S = max(60, _env_int("CORE_SEMANTIC_PROJECTION_INTERVAL_S", 300))
PROJECTION_BATCH_LIMIT = max(1, _env_int("CORE_SEMANTIC_PROJECTION_BATCH_LIMIT", 20))

_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_error": "",
    "last_summary": {},
}


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 500) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


def _parse_dt(value: Any) -> datetime | None:
    text = _safe_text(value, 80)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:120] or "semantic"


def _parse_task(row: dict) -> dict:
    raw = row.get("task", "")
    if isinstance(raw, dict):
        task = dict(raw)
    else:
        try:
            task = json.loads(raw) if raw else {}
        except Exception:
            task = {}
    if not isinstance(task, dict):
        task = {}
    return task


def _parse_blob(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _extractors() -> dict[str, dict]:
    return {
        "task_queue": {
            "select": "*",
            "text": lambda r: " | ".join(
                p for p in [
                    _parse_task(r).get("title", ""),
                    _parse_task(r).get("description", ""),
                    f"status={r.get('status', '')}",
                    f"priority={r.get('priority', '')}",
                    f"source={r.get('source', '')}",
                    _safe_text(r.get("next_step"), 120),
                    _safe_text(r.get("result"), 500),
                    _safe_text(r.get("error"), 500),
                ] if p
            ),
        },
        "sessions": {
            "select": "*",
            "text": lambda r: " | ".join(
                p for p in [
                    _safe_text(r.get("summary"), 500),
                    _safe_text(r.get("actions"), 600),
                    f"interface={r.get('interface', '')}",
                ] if p
            ),
        },
        "project_context": {
            "select": "*",
            "text": lambda r: " | ".join(
                p for p in [
                    _safe_text(r.get("project_id"), 120),
                    _safe_text(r.get("context_md"), 800),
                    f"prepared_by={r.get('prepared_by', '')}",
                    f"consumed={r.get('consumed', '')}",
                ] if p
            ),
        },
        "agentic_sessions": {
            "select": "*",
            "text": lambda r: " | ".join(
                p for p in [
                    _safe_text(r.get("session_id"), 120),
                    _safe_text(r.get("state"), 1200),
                    _safe_text(r.get("action_log"), 1200),
                    _safe_text(r.get("current_step"), 120),
                    f"step_index={r.get('step_index', '')}",
                ] if p
            ),
        },
        "reasoning_log": {
            "select": "*",
            "text": lambda r: " | ".join(
                p for p in [
                    _safe_text(r.get("domain"), 120),
                    _safe_text(r.get("action_planned"), 500),
                    _safe_text(r.get("action_taken"), 500),
                    _safe_text(r.get("outcome"), 500),
                    _safe_text(r.get("reasoning"), 1200),
                    f"confidence={r.get('confidence', '')}",
                ] if p
            ),
        },
        "backlog": {
            "select": "*",
            "text": lambda r: " | ".join(
                p for p in [
                    _safe_text(r.get("title"), 300),
                    _safe_text(r.get("description"), 800),
                    _safe_text(r.get("acceptance_criteria"), 500),
                    f"priority={r.get('priority', '')}",
                    f"status={r.get('status', '')}",
                ] if p
            ),
        },
    }


def _projection_identity(table: str, row: dict) -> tuple[str, str, str]:
    rid = _safe_text(row.get("id"), 80)
    domain = f"semantic:{table}"
    topic = f"{table}:{rid}" if rid else f"{table}:{_slugify(json.dumps(row, default=str)[:120])}"
    return domain, topic, rid


def _project_row(table: str, row: dict) -> dict:
    cfg = _extractors().get(table)
    if not cfg:
        return {"ok": False, "table": table, "reason": "unregistered_table"}

    text = _safe_text(cfg["text"](row), 4000)
    if not text:
        return {"ok": True, "table": table, "projected": False, "reason": "empty_text"}

    domain, topic, source_id = _projection_identity(table, row)
    source_ref = f"{table}:{source_id}" if source_id else table
    source_ts = _safe_text(row.get("updated_at") or row.get("created_at") or row.get("ts"), 80)
    if source_ref:
        try:
            existing = sb_get(
                "knowledge_base",
                f"select=id,updated_at,content&source_ref=eq.{source_ref}&limit=1",
                svc=True,
            ) or []
            if existing:
                existing_row = existing[0]
                existing_ts = _safe_text(existing_row.get("updated_at"), 80)
                if existing_ts and source_ts:
                    ex_dt = _parse_dt(existing_ts)
                    src_dt = _parse_dt(source_ts)
                    if ex_dt and src_dt and ex_dt >= src_dt:
                        return {
                            "ok": True,
                            "table": table,
                            "projected": False,
                            "reason": "already_current",
                            "source_ref": source_ref,
                            "topic": topic,
                        }
                elif existing_row.get("content"):
                    # If we cannot compare timestamps, keep the existing semantic row.
                    return {
                        "ok": True,
                        "table": table,
                        "projected": False,
                        "reason": "already_projected",
                        "source_ref": source_ref,
                        "topic": topic,
                    }
        except Exception as e:
            print(f"[SEM_PROJ] existing projection check failed for {source_ref}: {e}")
    payload = {
        "domain": domain,
        "topic": topic,
        "instruction": f"Semantic projection of {table} row {source_id}. Use this as retrieval memory for CORE autonomy.",
        "content": text,
        "confidence": "proven",
        "source": "semantic_projection",
        "source_type": "semantic_projection",
        "source_ref": source_ref,
        "tags": ["semantic_projection", table],
        "active": True,
    }
    ok = sb_upsert("knowledge_base", payload, on_conflict="domain,topic")
    if not ok:
        return {"ok": False, "table": table, "projected": False, "reason": "kb_upsert_failed", "source_ref": payload["source_ref"]}

    return {
        "ok": True,
        "table": table,
        "projected": True,
        "source_ref": payload["source_ref"],
        "topic": topic,
    }


def run_semantic_projection_cycle(max_rows: int = PROJECTION_BATCH_LIMIT) -> dict:
    if not PROJECTION_ENABLED:
        return {"ok": False, "enabled": False, "message": "CORE_SEMANTIC_PROJECTION_ENABLED=false"}

    try:
        max_rows = int(max_rows)
    except Exception:
        max_rows = PROJECTION_BATCH_LIMIT
    if max_rows <= 0:
        return {
            "ok": True,
            "enabled": True,
            "started_at": _utcnow(),
            "finished_at": _utcnow(),
            "processed": 0,
            "projected": 0,
            "skipped": 0,
            "details": [],
            "note": "No-op cycle requested (max_rows <= 0).",
        }

    with _lock:
        if _state["running"]:
            return {"ok": False, "busy": True, "message": "semantic projection cycle already running"}
        _state["running"] = True

    started_at = _utcnow()
    details: list[dict] = []
    projected = 0
    skipped = 0
    errors: list[dict] = []
    try:
        for table, cfg in _extractors().items():
            try:
                rows = sb_get(
                    table,
                    f"select={cfg['select']}&order=id.desc&limit={max(1, min(max_rows, 50))}",
                    svc=True,
                ) or []
            except Exception as e:
                errors.append({"table": table, "error": str(e)})
                continue
            for row in rows[:max(1, min(max_rows, 50))]:
                try:
                    result = _project_row(table, row)
                    details.append(result)
                    if result.get("projected"):
                        projected += 1
                    else:
                        skipped += 1
                except Exception as e:
                    errors.append({"table": table, "row_id": row.get("id"), "error": str(e)})
                    skipped += 1
        summary = {
            "started_at": started_at,
            "finished_at": _utcnow(),
            "processed": len(details),
            "projected": projected,
            "skipped": skipped,
            "details": details,
            "errors": errors,
        }
        _state["last_run_at"] = summary["finished_at"]
        _state["last_summary"] = summary
        _state["last_error"] = ""
        return {"ok": True, "enabled": True, **summary}
    except Exception as e:
        _state["last_error"] = str(e)
        return {"ok": False, "enabled": True, "error": str(e), "details": details, "errors": errors}
    finally:
        with _lock:
            _state["running"] = False


def semantic_projection_loop() -> None:
    while PROJECTION_ENABLED:
        try:
            cycle = run_semantic_projection_cycle(max_rows=PROJECTION_BATCH_LIMIT)
            if not cycle.get("ok") and cycle.get("busy"):
                time.sleep(min(30, PROJECTION_INTERVAL_S))
            else:
                time.sleep(PROJECTION_INTERVAL_S)
        except Exception as e:
            _state["last_error"] = str(e)
            time.sleep(min(60, PROJECTION_INTERVAL_S))


def semantic_projection_status() -> dict:
    counts = {}
    for table in _extractors().keys():
        counts[table] = 0
        try:
            import httpx
             param($m)
        $line = $m.Groups[1].Value
        if ($line -match '_env_int' -or $line -match '_env_float') { return $m.Value }
        return 'from core_config import ' + $line + ', _env_int, _env_float'
    
            r = httpx.get(
                f"{SUPABASE_URL}/rest/v1/knowledge_base?select=id&domain=eq.semantic%3A{table}&limit=1",
                headers={**_sbh_count_svc(), "Prefer": "count=exact"},
                timeout=15,
            )
            cr = r.headers.get("content-range", "*/0")
            counts[table] = int(cr.split("/")[-1]) if "/" in cr else 0
        except Exception:
            counts[table] = 0
    return {
        "ok": True,
        "enabled": PROJECTION_ENABLED,
        "running": _state["running"],
        "interval_seconds": PROJECTION_INTERVAL_S,
        "batch_limit": PROJECTION_BATCH_LIMIT,
        "last_run_at": _state["last_run_at"],
        "last_error": _state["last_error"],
        "last_summary": _state["last_summary"],
        "projected_domains": counts,
    }


def register_tools() -> None:
    try:
        from core_tools import TOOLS
    except Exception as e:
        print(f"[SEM_PROJ] tool registration skipped: {e}")
        return

    if "semantic_projection_run" not in TOOLS:
        TOOLS["semantic_projection_run"] = {
            "fn": t_semantic_projection_run,
            "perm": "WRITE",
            "args": [
                {"name": "max_rows", "type": "string", "description": "Max rows per table to project in this cycle (default 20)"},
            ],
            "desc": "Run one silent semantic projection cycle. Mirrors important raw rows into knowledge_base and embeds them.",
        }
    if "semantic_projection_status" not in TOOLS:
        TOOLS["semantic_projection_status"] = {
            "fn": t_semantic_projection_status,
            "perm": "READ",
            "args": [],
            "desc": "Return silent semantic projection worker status and coverage counts.",
        }


def t_semantic_projection_run(max_rows: str = "20") -> dict:
    try:
        lim = int(max_rows) if max_rows else PROJECTION_BATCH_LIMIT
    except Exception:
        lim = PROJECTION_BATCH_LIMIT
    return run_semantic_projection_cycle(max_rows=lim)


def t_semantic_projection_status() -> dict:
    return semantic_projection_status()


register_tools()
