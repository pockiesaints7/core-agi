"""core_evolution_autonomy.py -- synthesize evolution_queue into task_queue.

This worker is the bridge between signal discovery and execution.
It reads pending evolutions, turns each one into a concrete improvement task,
and leaves a clear provenance trail so CORE can later automate deeper code
changes without losing traceability.

Phase 1:
- synthesize evolution_queue rows into task_queue tasks
- classify the follow-up work as knowledge, behavioral, or architecture/code
- notify the owner with one production-grade summary per cycle

Phase 2, later:
- consume these synthesized tasks with a dedicated code-evolution worker
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any

import httpx

from core_config import SUPABASE_URL, _sbh_count_svc, sb_get, sb_patch, sb_post
from core_github import notify
from core_work_taxonomy import build_autonomy_contract

AUTONOMY_ENABLED = os.getenv("CORE_EVOLUTION_AUTONOMY_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_INTERVAL_S = max(120, int(os.getenv("CORE_EVOLUTION_AUTONOMY_INTERVAL_S", "600")))
AUTONOMY_BATCH_LIMIT = max(1, int(os.getenv("CORE_EVOLUTION_AUTONOMY_BATCH_LIMIT", "3")))
AUTONOMY_NOTIFY = os.getenv("CORE_EVOLUTION_AUTONOMY_NOTIFY", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
TASK_SOURCE = "improvement"

_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_summary": {},
    "last_error": "",
    "last_evolution_id": "",
}


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 240) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, default=str)
        return value
    except Exception:
        return str(value)


def _count_rows(table: str, qs: str) -> int:
    try:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
            headers=_sbh_count_svc(),
            timeout=15,
        )
        cr = r.headers.get("content-range", "*/0")
        return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return 0


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value[:100] or "evolution"


def _row_autonomy(row: dict) -> dict:
    autonomy = row.get("autonomy") or {}
    if isinstance(autonomy, str):
        try:
            autonomy = json.loads(autonomy)
        except Exception:
            autonomy = {}
    if not isinstance(autonomy, dict) or not autonomy:
        diff = row.get("diff_content") or {}
        if isinstance(diff, str):
            try:
                diff = json.loads(diff)
            except Exception:
                diff = {}
        if isinstance(diff, dict):
            autonomy = diff.get("autonomy") or autonomy
    return autonomy if isinstance(autonomy, dict) else {}


def _priority_from_row(row: dict, strategy: dict) -> int:
    try:
        if row.get("priority") is not None:
            return int(row.get("priority"))
    except Exception:
        pass
    return int(strategy.get("priority") or 3)


def _parse_change_summary(row: dict) -> str:
    summary = _safe_text(row.get("change_summary") or row.get("summary") or "", 500)
    if summary:
        return summary
    diff = row.get("diff_content")
    if diff:
        try:
            if isinstance(diff, str):
                parsed = json.loads(diff)
                summary = _safe_text(parsed.get("change_summary") or parsed.get("summary") or parsed.get("title") or "", 500)
            elif isinstance(diff, dict):
                summary = _safe_text(diff.get("change_summary") or diff.get("summary") or diff.get("title") or "", 500)
        except Exception:
            summary = ""
    return summary or "Unnamed evolution"


def _extract_kind(change_type: str, summary: str, recommendation: str, autonomy: dict | None = None) -> dict:
    autonomy = autonomy if isinstance(autonomy, dict) else {}
    contract = build_autonomy_contract(
        summary,
        description=f"{change_type}\n{summary}\n{recommendation}",
        source=_safe_text(autonomy.get("source") or change_type or "evolution_queue", 80),
        autonomy=autonomy,
        context="evolution_autonomy",
    )
    if not contract.get("kind"):
        contract["kind"] = "architecture_proposal"
    if not contract.get("work_track"):
        contract["work_track"] = "proposal_only"
    if not contract.get("execution_mode"):
        contract["execution_mode"] = "proposal"
    if not contract.get("expected_artifact"):
        contract["expected_artifact"] = "evolution_queue"
    if not contract.get("task_group"):
        contract["task_group"] = "architecture"
    return contract


def _task_exists(evolution_id: str, title: str) -> bool:
    rows = sb_get(
        "task_queue",
        "select=id,task,status,source&status=in.(pending,in_progress)&source=in.(improvement,self_assigned)&limit=200",
        svc=True,
    ) or []
    for row in rows:
        blob = row.get("task", "")
        try:
            task = json.loads(blob) if isinstance(blob, str) else blob
        except Exception:
            task = {}
        if not isinstance(task, dict):
            task = {}
        autonomy = task.get("autonomy") or {}
        if isinstance(autonomy, str):
            try:
                autonomy = json.loads(autonomy)
            except Exception:
                autonomy = {}
        if str(autonomy.get("evolution_id") or "") == str(evolution_id):
            return True
        if _safe_text(task.get("title"), 200).strip().lower() == title.strip().lower():
            return True
    return False


def _build_task_payload(row: dict, strategy: dict) -> dict:
    evolution_id = str(row.get("id") or "")
    summary = _parse_change_summary(row)
    change_type = _safe_text(row.get("change_type") or "unknown", 40)
    recommendation = _safe_text(row.get("recommendation") or "", 800)
    confidence = row.get("confidence")
    try:
        conf = float(confidence) if confidence is not None else 0.8
    except Exception:
        conf = 0.8
    title = f"[EVO] {summary[:120]}"
    task = {
        "title": title,
        "description": (
            f"Evolution #{evolution_id} | type={change_type} | conf={conf:.2f} | "
            f"group={strategy['task_group']} | track={strategy.get('work_track')} | impact={_safe_text(row.get('impact') or '', 80)}\n"
            f"Summary: {summary}\n"
            f"Recommendation: {recommendation or 'review and decide'}"
        ),
        "source": TASK_SOURCE,
        "evolution_id": evolution_id,
        "change_type": change_type,
        "autonomy": {
            "kind": strategy["kind"],
            "origin": "evolution_queue",
            "source": "evolution_queue",
            "evolution_id": evolution_id,
            "evolution_status": _safe_text(row.get("status") or "pending", 40),
            "work_track": strategy.get("work_track", "proposal_only"),
            "execution_mode": strategy.get("execution_mode", "proposal"),
            "task_group": strategy["task_group"],
            "artifact_domain": strategy["artifact_domain"],
            "expected_artifact": strategy["expected_artifact"],
            "verification": strategy.get("verification") or "task_queue checkpoint + durable artifact",
            "route": "task_autonomy",
            "specialized_worker": strategy.get("specialized_worker") or "task_autonomy",
        },
    }
    return {
        "task": json.dumps(task, default=str),
        "priority": _priority_from_row(row, strategy),
        "source": TASK_SOURCE,
        "evolution_id": evolution_id,
        "title": title,
        "strategy": strategy,
    }


def _notify_cycle(summary: dict) -> None:
    if not AUTONOMY_NOTIFY:
        return
    processed = summary.get("processed", 0)
    created = summary.get("queued", 0)
    duplicates = summary.get("duplicates", 0)
    failures = summary.get("failures", 0)
    track_counts = summary.get("track_counts") or {}
    parts = [
        "<b>EVOLUTION AUTONOMY CYCLE</b>",
        f"Window: {summary.get('started_at', '?')} -> {summary.get('finished_at', '?')}",
        f"Processed: {processed} | Created: {created} | Duplicates: {duplicates} | Failures: {failures}",
        f"Skipped total: {summary.get('skipped', 0)} | Pending evolutions remaining: {summary.get('pending_remaining', 0)}",
        f"Task queue: pending {summary.get('pending_improvement_tasks', '?')}",
    ]
    if track_counts:
        parts.append("Tracks: " + ", ".join(f"{k}={v}" for k, v in sorted(track_counts.items())))
    details = summary.get("details") or []
    for item in details[:5]:
        outcome = item.get("outcome") or ("created" if item.get("task_created") else "duplicate")
        task_label = _safe_text(item.get("task_title") or item.get("change_summary") or "", 180)
        parts.append(
            f"- #{item.get('evolution_id')} [{item.get('change_type')}] {outcome}: {task_label} "
            f"({item.get('task_group')} / {item.get('work_track') or 'unknown'})"
        )
        reason = _safe_text(item.get("reason") or item.get("error") or "", 220)
        if reason:
            parts.append(f"  reason: {reason}")
    try:
        notify("\n".join(parts))
    except Exception as e:
        print(f"[EVO_AUTONOMY] notify failed: {e}")


def process_evolution_row(row: dict) -> dict:
    evolution_id = str(row.get("id") or "")
    summary = _parse_change_summary(row)
    change_type = _safe_text(row.get("change_type") or "unknown", 40)
    recommendation = _safe_text(row.get("recommendation") or "", 800)
    autonomy = _row_autonomy(row)
    strategy = _extract_kind(change_type, summary, recommendation, autonomy=autonomy)
    title = f"[EVO] {summary[:120]}"

    if _task_exists(evolution_id, title):
        sb_patch("evolution_queue", f"id=eq.{evolution_id}", {
            "status": "synthesized",
            "updated_at": _utcnow(),
        })
        return {
            "ok": True,
            "evolution_id": evolution_id,
            "change_type": change_type,
            "task_created": False,
            "outcome": "duplicate",
            "task_title": title,
            "task_group": strategy["task_group"],
            "work_track": strategy.get("work_track", ""),
            "execution_mode": strategy.get("execution_mode", ""),
            "reason": "duplicate task already exists",
        }

    payload = _build_task_payload(row, strategy)
    task_ok = sb_post("task_queue", {
        "task": payload["task"],
        "status": "pending",
        "priority": payload["priority"],
        "source": payload["source"],
    })
    task_id = ""
    if task_ok:
        created = sb_get(
            "task_queue",
            f"select=id&source=eq.{TASK_SOURCE}&status=eq.pending&order=created_at.desc&limit=1",
            svc=True,
        ) or []
        if created:
            task_id = str(created[0].get("id") or "")

        sb_patch("evolution_queue", f"id=eq.{evolution_id}", {
            "status": "synthesized",
            "updated_at": _utcnow(),
        })
    else:
        sb_patch("evolution_queue", f"id=eq.{evolution_id}", {
            "status": "synthesis_failed",
            "updated_at": _utcnow(),
        })

    return {
        "ok": bool(task_ok),
        "evolution_id": evolution_id,
        "change_type": change_type,
        "task_created": bool(task_ok),
        "outcome": "created" if task_ok else "error",
        "task_id": task_id,
        "task_title": payload["title"],
        "task_group": strategy["task_group"],
        "work_track": strategy.get("work_track", ""),
        "execution_mode": strategy.get("execution_mode", ""),
        "strategy": strategy,
        "task_payload": _jsonable(json.loads(payload["task"])),
    }


def run_evolution_autonomy_cycle(max_evolutions: int = AUTONOMY_BATCH_LIMIT) -> dict:
    if not AUTONOMY_ENABLED:
        return {"ok": False, "enabled": False, "message": "CORE_EVOLUTION_AUTONOMY_ENABLED=false"}

    try:
        max_evolutions = int(max_evolutions)
    except Exception:
        max_evolutions = AUTONOMY_BATCH_LIMIT
    if max_evolutions <= 0:
        return {
            "ok": True,
            "enabled": True,
            "started_at": _utcnow(),
            "finished_at": _utcnow(),
            "processed": 0,
            "queued": 0,
            "skipped": 0,
            "pending_remaining": 0,
            "details": [],
            "note": "No-op cycle requested (max_evolutions <= 0).",
        }

    with _lock:
        if _state["running"]:
            return {"ok": False, "busy": True, "message": "evolution autonomy cycle already running"}
        _state["running"] = True

    started_at = _utcnow()
    details: list[dict] = []
    queued = 0
    duplicates = 0
    failures = 0
    skipped = 0
    errors: list[dict] = []
    track_counts: dict[str, int] = {}
    try:
        rows = sb_get(
            "evolution_queue",
            f"select=id,change_type,change_summary,recommendation,confidence,impact,status,diff_content,pattern_key"
            f"&status=eq.pending&order=confidence.desc&limit={max(1, min(max_evolutions, 10))}",
            svc=True,
        ) or []
        for row in rows[:max(1, min(max_evolutions, 10))]:
            try:
                result = process_evolution_row(row)
                details.append(result)
                track = _safe_text(result.get("work_track") or "unknown", 40)
                track_counts[track] = track_counts.get(track, 0) + 1
                if result.get("task_created"):
                    queued += 1
                elif result.get("outcome") == "duplicate":
                    duplicates += 1
                else:
                    failures += 1
                    skipped += 1
            except Exception as e:
                errors.append({"evolution_id": row.get("id"), "error": str(e)})
                failures += 1
                skipped += 1
        pending_remaining = _count_rows("evolution_queue", "select=id&status=eq.pending")
        task_pending = _count_rows("task_queue", "select=id&status=eq.pending&source=eq.improvement")
        summary = {
            "started_at": started_at,
            "finished_at": _utcnow(),
            "processed": len(rows),
            "queued": queued,
            "duplicates": duplicates,
            "failures": failures,
            "skipped": skipped,
            "pending_remaining": pending_remaining,
            "pending_improvement_tasks": task_pending,
            "track_counts": track_counts,
            "details": details,
            "errors": errors,
        }
        _state["last_run_at"] = summary["finished_at"]
        _state["last_summary"] = summary
        _state["last_error"] = ""
        if queued or skipped:
            try:
                sb_post("sessions", {
                    "summary": f"[state_update] evolution_autonomy_last_run: {_state['last_run_at']}",
                    "actions": [
                        f"evolution_autonomy cycle processed={len(rows)} created={queued} duplicates={duplicates} failures={failures}",
                    ],
                    "interface": "evolution_autonomy",
                })
            except Exception:
                pass
        _notify_cycle(summary)
        return {"ok": True, "enabled": True, **summary}
    except Exception as e:
        _state["last_error"] = str(e)
        return {"ok": False, "enabled": True, "error": str(e), "details": details, "errors": errors}
    finally:
        with _lock:
            _state["running"] = False


def evolution_autonomy_loop() -> None:
    while AUTONOMY_ENABLED:
        try:
            cycle = run_evolution_autonomy_cycle(max_evolutions=AUTONOMY_BATCH_LIMIT)
            if not cycle.get("ok") and cycle.get("busy"):
                time.sleep(min(60, AUTONOMY_INTERVAL_S))
            else:
                time.sleep(AUTONOMY_INTERVAL_S)
        except Exception as e:
            _state["last_error"] = str(e)
            time.sleep(min(120, AUTONOMY_INTERVAL_S))


def evolution_autonomy_status() -> dict:
    pending = _count_rows("evolution_queue", "select=id&status=eq.pending")
    synthesized = _count_rows("evolution_queue", "select=id&status=eq.synthesized")
    task_pending = _count_rows("task_queue", "select=id&status=eq.pending&source=eq.improvement")
    return {
        "ok": True,
        "enabled": AUTONOMY_ENABLED,
        "running": _state["running"],
        "interval_seconds": AUTONOMY_INTERVAL_S,
        "batch_limit": AUTONOMY_BATCH_LIMIT,
        "task_source": TASK_SOURCE,
        "last_run_at": _state["last_run_at"],
        "last_error": _state["last_error"],
        "pending_evolutions": pending,
        "synthesized_evolutions": synthesized,
        "pending_improvement_tasks": task_pending,
        "track_counts": _state["last_summary"].get("track_counts", {}),
        "last_summary": _state["last_summary"],
    }


def register_tools() -> None:
    try:
        from core_tools import TOOLS
    except Exception as e:
        print(f"[EVO_AUTONOMY] tool registration skipped: {e}")
        return

    if "evolution_autonomy_run" not in TOOLS:
        TOOLS["evolution_autonomy_run"] = {
            "fn": t_evolution_autonomy_run,
            "perm": "WRITE",
            "args": [
                {"name": "max_evolutions", "type": "string", "description": "Max evolutions to synthesize in this cycle (default 3)"},
            ],
            "desc": "Run one evolution -> task synthesis cycle. Converts pending evolutions into improvement tasks with durable audit metadata.",
        }
    if "evolution_autonomy_status" not in TOOLS:
        TOOLS["evolution_autonomy_status"] = {
            "fn": t_evolution_autonomy_status,
            "perm": "READ",
            "args": [],
            "desc": "Return evolution autonomy worker status and queue depth.",
        }


def t_evolution_autonomy_run(max_evolutions: str = "3") -> dict:
    try:
        lim = int(max_evolutions) if max_evolutions else AUTONOMY_BATCH_LIMIT
    except Exception:
        lim = AUTONOMY_BATCH_LIMIT
    return run_evolution_autonomy_cycle(max_evolutions=lim)


def t_evolution_autonomy_status() -> dict:
    return evolution_autonomy_status()


register_tools()
