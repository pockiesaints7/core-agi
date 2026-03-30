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
import html
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any

import httpx
from core_github import notify
from core_queue_cursor import build_seek_filter, cursor_from_row
from core_work_taxonomy import build_autonomy_contract

AUTONOMY_ENABLED = os.getenv("CORE_EVOLUTION_AUTONOMY_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_INTERVAL_S = max(120, _env_int("CORE_EVOLUTION_AUTONOMY_INTERVAL_S", "600"))
AUTONOMY_BATCH_LIMIT = max(1, _env_int("CORE_EVOLUTION_AUTONOMY_BATCH_LIMIT", "3"))
AUTONOMY_NOTIFY = os.getenv("CORE_EVOLUTION_AUTONOMY_NOTIFY", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
AUTONOMY_BACKLOG_MONITOR = os.getenv("CORE_EVOLUTION_AUTONOMY_BACKLOG_MONITOR", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
AUTONOMY_BACKLOG_WINDOW = max(3, _env_int("CORE_EVOLUTION_AUTONOMY_BACKLOG_WINDOW", "3"))
AUTONOMY_BACKLOG_MIN_GROWTH = max(10, _env_int("CORE_EVOLUTION_AUTONOMY_BACKLOG_MIN_GROWTH", "25"))
AUTONOMY_BACKLOG_ALERT_COOLDOWN_S = max(300, _env_int("CORE_EVOLUTION_AUTONOMY_BACKLOG_ALERT_COOLDOWN_S", "3600"))
TASK_SOURCE = "improvement"

_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_summary": {},
    "last_error": "",
    "last_evolution_id": "",
    "backlog_samples": [],
    "last_backlog_monitor": {},
    "last_backlog_alert_at": "",
    "queue_cursor": {},
}

QUEUE_ORDER = (("confidence", "desc"), ("created_at", "asc"), ("id", "asc"))
QUEUE_PAGE_LIMIT = 250


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _safe_text(value: Any, limit: int = 240) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()[:limit]


def _safe_html(value: Any, limit: int = 240) -> str:
    return html.escape(_safe_text(value, limit), quote=False)


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
    # First check the explicit evolution id across the full task history.
    try:
        rows = sb_get(
            "task_queue",
            (
                "select=id,task,status,source"
                f"&source=in.(improvement,self_assigned)"
                f"&task=ilike.*\"evolution_id\":\"{_safe_text(evolution_id, 40)}\"*"
                "&limit=20"
            ),
            svc=True,
        ) or []
        if rows:
            return True
    except Exception:
        pass

    # Fallback: exact title match across the full task history. This catches
    # re-runs of the same evolution after the earlier task is no longer open.
    try:
        from urllib.parse import quote as _urlquote
        title_q = _urlquote(title.strip(), safe="")
        rows = sb_get(
            "task_queue",
            (
                "select=id,task,status,source"
                f"&source=in.(improvement,self_assigned)"
                f"&task=ilike.*{title_q}*"
                "&limit=50"
            ),
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
            if _safe_text(task.get("title"), 200).strip().lower() == title.strip().lower():
                return True
    except Exception:
        pass
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
            "review_scope": strategy.get("review_scope") or ("worker" if strategy.get("work_track") in {"db_only", "behavioral_rule", "research"} else "owner"),
            "owner_only": bool(strategy.get("owner_only", strategy.get("review_scope") == "owner")),
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
        "<b>EVOLUTION AUTONOMY</b>",
        "Cycle summary",
        f"Window: {_safe_html(summary.get('started_at', '?'), 60)} -> {_safe_html(summary.get('finished_at', '?'), 60)}",
        f"Processed: {processed} | Created: {created} | Duplicates: {duplicates} | Failures: {failures}",
        f"Skipped total: {summary.get('skipped', 0)} | Pending evolutions remaining: {summary.get('pending_remaining', 0)}",
        f"Task queue: pending {summary.get('pending_improvement_tasks', '?')}",
    ]
    if track_counts:
        parts.append("Tracks: " + ", ".join(f"{k}={v}" for k, v in sorted(track_counts.items())))
    details = summary.get("details") or []
    for item in details[:5]:
        outcome = item.get("outcome") or ("created" if item.get("task_created") else "duplicate")
        task_label = _safe_html(item.get("task_title") or item.get("change_summary") or "", 180)
        parts.append(
            f"- #{item.get('evolution_id')} [{_safe_html(item.get('change_type') or 'unknown', 40)}] "
            f"{_safe_html(outcome, 30)}: {task_label} "
            f"({_safe_html(item.get('task_group') or '', 40)} / {_safe_html(item.get('work_track') or 'unknown', 40)})"
        )
        reason = _safe_html(item.get("reason") or item.get("error") or "", 220)
        if reason:
            parts.append(f"  reason: {reason}")
    try:
        notify("\n".join(parts))
    except Exception as e:
        print(f"[EVO_AUTONOMY] notify failed: {e}")


def _monitor_backlog(summary: dict) -> dict:
    if not AUTONOMY_BACKLOG_MONITOR:
        monitor = {
            "enabled": False,
            "trend": "disabled",
            "window": AUTONOMY_BACKLOG_WINDOW,
            "min_growth": AUTONOMY_BACKLOG_MIN_GROWTH,
            "cooldown_seconds": AUTONOMY_BACKLOG_ALERT_COOLDOWN_S,
            "sample_count": len(_state.get("backlog_samples") or []),
            "alert": "",
        }
        _state["last_backlog_monitor"] = monitor
        return monitor

    finished_at = summary.get("finished_at") or _utcnow()
    pending = int(summary.get("pending_remaining") or 0)
    task_pending = int(summary.get("pending_improvement_tasks") or 0)
    samples = list(_state.get("backlog_samples") or [])
    samples.append({
        "ts": finished_at,
        "pending": pending,
        "task_pending": task_pending,
    })
    samples = samples[-max(12, AUTONOMY_BACKLOG_WINDOW * 4):]
    _state["backlog_samples"] = samples

    window_samples = samples[-AUTONOMY_BACKLOG_WINDOW:]
    pending_series = [int(item.get("pending") or 0) for item in window_samples]
    task_series = [int(item.get("task_pending") or 0) for item in window_samples]
    growth = pending_series[-1] - pending_series[0] if len(pending_series) >= 2 else 0
    task_growth = task_series[-1] - task_series[0] if len(task_series) >= 2 else 0
    sustained_growth = len(pending_series) >= AUTONOMY_BACKLOG_WINDOW and all(b > a for a, b in zip(pending_series, pending_series[1:]))
    stable_task_growth = len(task_series) >= AUTONOMY_BACKLOG_WINDOW and all(b >= a for a, b in zip(task_series, task_series[1:]))
    trend = "stable"
    alert = ""

    if sustained_growth and growth >= AUTONOMY_BACKLOG_MIN_GROWTH:
        trend = "rising"
    elif len(pending_series) >= 2 and pending_series[-1] < pending_series[0]:
        trend = "falling"
    elif len(pending_series) >= 2 and pending_series[-1] == pending_series[0]:
        trend = "flat"

    if trend == "rising":
        alert = (
            f"Evolution backlog rising across {len(pending_series)} cycles: "
            f"{pending_series[0]} -> {pending_series[-1]} (Î”{growth}); "
            f"follow-up tasks {task_series[0] if task_series else 0} -> {task_series[-1] if task_series else 0} "
            f"(Î”{task_growth})."
        )
        last_alert = _state.get("last_backlog_alert_at") or ""
        cooldown_ok = True
        if last_alert:
            try:
                cooldown_ok = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(last_alert)).total_seconds() >= AUTONOMY_BACKLOG_ALERT_COOLDOWN_S
            except Exception:
                cooldown_ok = True
        if cooldown_ok:
            try:
                notify(
                    "<b>EVOLUTION BACKLOG MONITOR</b>\n"
                    f"Window: {window_samples[0].get('ts', '?')} -> {window_samples[-1].get('ts', '?')}\n"
                    f"Pending evolutions: {pending_series[0]} -> {pending_series[-1]} (Î”{growth})\n"
                    f"Follow-up tasks: {task_series[0] if task_series else 0} -> {task_series[-1] if task_series else 0} (Î”{task_growth})\n"
                    f"Action: keep current drain rate unless this trend persists after another window."
                )
                _state["last_backlog_alert_at"] = finished_at
            except Exception as e:
                print(f"[EVO_AUTONOMY] backlog monitor notify failed: {e}")

    monitor = {
        "enabled": True,
        "trend": trend,
        "window": AUTONOMY_BACKLOG_WINDOW,
        "min_growth": AUTONOMY_BACKLOG_MIN_GROWTH,
        "cooldown_seconds": AUTONOMY_BACKLOG_ALERT_COOLDOWN_S,
        "sample_count": len(samples),
        "pending_series": pending_series,
        "task_series": task_series,
        "growth": growth,
        "task_growth": task_growth,
        "alert": alert,
        "sustained_growth": sustained_growth,
        "stable_task_growth": stable_task_growth,
        "last_alert_at": _state.get("last_backlog_alert_at", ""),
    }
    _state["last_backlog_monitor"] = monitor
    return monitor


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
            "status": "rejected",
            "recommendation": "Duplicate task already exists",
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
        attempted = 0
        claimed = 0
        inspected = 0
        cursor = _state.get("queue_cursor") or {}
        page_limit = max(100, min(max_evolutions * 80, QUEUE_PAGE_LIMIT))
        while True:
            cursor_filter = build_seek_filter(cursor, QUEUE_ORDER)
            qs = (
                "select=id,change_type,change_summary,recommendation,confidence,impact,status,diff_content,pattern_key,created_at"
                f"&status=eq.pending"
                f"{('&' + cursor_filter) if cursor_filter else ''}"
                f"&order=confidence.desc,created_at.asc,id.asc&limit={page_limit}"
            )
            rows = sb_get("evolution_queue", qs, svc=True) or []
            if not rows:
                if cursor:
                    cursor = {}
                    _state["queue_cursor"] = {}
                break
            for row in rows:
                try:
                    if claimed >= max_evolutions:
                        break
                    inspected += 1
                    cursor = cursor_from_row(row, QUEUE_ORDER)
                    _state["queue_cursor"] = cursor
                    result = process_evolution_row(row)
                    details.append(result)
                    attempted += 1
                    track = _safe_text(result.get("work_track") or "unknown", 40)
                    track_counts[track] = track_counts.get(track, 0) + 1
                    if result.get("task_created"):
                        queued += 1
                        claimed += 1
                    elif result.get("outcome") == "duplicate":
                        duplicates += 1
                    else:
                        failures += 1
                        skipped += 1
                except Exception as e:
                    errors.append({"evolution_id": row.get("id"), "error": str(e)})
                    failures += 1
                    skipped += 1
            if claimed >= max_evolutions:
                break
            if len(rows) < page_limit:
                cursor = {}
                _state["queue_cursor"] = {}
                break
        pending_remaining = _count_rows("evolution_queue", "select=id&status=eq.pending")
        task_pending = _count_rows("task_queue", "select=id&status=eq.pending&source=eq.improvement")
        summary = {
            "started_at": started_at,
            "finished_at": _utcnow(),
            "processed": attempted,
            "inspected": inspected,
            "queued": queued,
            "duplicates": duplicates,
            "failures": failures,
            "skipped": skipped,
            "pending_remaining": pending_remaining,
            "pending_improvement_tasks": task_pending,
            "track_counts": track_counts,
            "details": details,
            "errors": errors,
            "queue_cursor": _state.get("queue_cursor") or {},
        }
        summary["backlog_monitor"] = _monitor_backlog(summary)
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
        "backlog_monitor": _state.get("last_backlog_monitor", {}),
        "task_source": TASK_SOURCE,
        "last_run_at": _state["last_run_at"],
        "last_error": _state["last_error"],
        "pending_evolutions": pending,
        "synthesized_evolutions": synthesized,
        "pending_improvement_tasks": task_pending,
        "track_counts": _state["last_summary"].get("track_counts", {}),
        "last_summary": _state["last_summary"],
        "queue_cursor": _state.get("queue_cursor") or {},
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


