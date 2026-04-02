from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any
from pathlib import Path

from core_config import _env_int, sb_get, sb_post
from core_github import notify
from core_proposal_router import proposal_router_status
from core_task_autonomy import autonomy_status
from core_research_autonomy import research_autonomy_status
from core_code_autonomy import code_autonomy_status
from core_integration_autonomy import integration_autonomy_status
from core_evolution_autonomy import evolution_autonomy_status

AUTONOMY_DIGEST_ENABLED = os.getenv("CORE_AUTONOMY_DIGEST_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off"
}
AUTONOMY_DIGEST_INTERVAL_S = max(3600, _env_int("CORE_AUTONOMY_DIGEST_INTERVAL_S", "86400"))
AUTONOMY_DIGEST_CHECK_S = max(300, _env_int("CORE_AUTONOMY_DIGEST_CHECK_S", "900"))
AUTONOMY_DIGEST_WINDOW_HOURS = max(1, _env_int("CORE_AUTONOMY_DIGEST_WINDOW_HOURS", "24"))
AUTONOMY_DIGEST_LIMIT = max(1, _env_int("CORE_AUTONOMY_DIGEST_LIMIT", "5000"))

_INTERFACES = (
    "task_autonomy",
    "research_autonomy",
    "code_autonomy",
    "integration_autonomy",
    "evolution_autonomy",
)

_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_at": "",
    "last_error": "",
    "last_summary": {},
}
_STATE_FILE = Path(__file__).resolve().parent / ".runtime" / "autonomy_digest_state.json"

_NUMERIC_KEYS = {
    "processed",
    "completed",
    "deferred",
    "failed",
    "blocked",
    "errors",
    "skipped",
    "proposed",
    "duplicates",
    "queued",
    "pending",
    "pending_now",
    "in_progress_now",
    "pending_proposals_now",
    "pending_remaining",
    "pending_improvement_tasks",
    "follow_up_queued",
}


def _utcnow() -> str:
    return datetime.utcnow().isoformat()


def _window_start(hours: int) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        try:
            return int(float(value))
        except Exception:
            return 0


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_action(actions: Any) -> Any:
    if isinstance(actions, list):
        return actions[0] if actions else {}
    return actions


def _parse_session_payload(row: dict) -> dict:
    payload = _as_dict(_first_action(row.get("actions")))
    if payload:
        return payload
    summary = _as_dict(row.get("summary"))
    if summary:
        return summary
    text = str(_first_action(row.get("actions")) or "").strip()
    if not text:
        return {}
    payload = {}
    for key in sorted(_NUMERIC_KEYS):
        m = re.search(rf"\b{re.escape(key)}=(\d+)", text)
        if m:
            payload[key] = int(m.group(1))
    return payload


def _sum_field(rows: list[dict], field: str) -> int:
    total = 0
    for row in rows:
        total += _safe_int(row.get(field))
    return total


def _load_local_digest_state() -> dict[str, Any]:
    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_local_digest_state(payload: dict[str, Any]) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception:
        pass


def _aggregate_sessions() -> dict[str, dict[str, int]]:
    window_start = _window_start(AUTONOMY_DIGEST_WINDOW_HOURS)
    rows = sb_get(
        "sessions",
        (
            "select=interface,summary,actions,created_at"
            f"&created_at=gte.{window_start}"
            f"&interface=in.({','.join(_INTERFACES)})"
            "&order=created_at.desc"
            f"&limit={AUTONOMY_DIGEST_LIMIT}"
        ),
        svc=True,
    ) or []

    aggregated: dict[str, Counter] = {name: Counter() for name in _INTERFACES}
    for row in rows:
        interface = str(row.get("interface") or "").strip()
        if interface not in aggregated:
            continue
        payload = _parse_session_payload(row)
        for key, value in payload.items():
            if key in _NUMERIC_KEYS:
                aggregated[interface][key] += _safe_int(value)
    return {k: dict(v) for k, v in aggregated.items()}


def _count_rows(table: str, qs: str) -> int:
    try:
        rows = sb_get(table, qs, svc=True) or []
        return len(rows)
    except Exception:
        return 0


def _current_last_digest_at() -> str:
    local_state = _load_local_digest_state()
    local_last = str(local_state.get("last_digest_at") or "").strip()
    if local_last:
        return local_last
    try:
        rows = sb_get(
            "sessions",
            "select=created_at&interface=eq.autonomy_digest&order=created_at.desc&limit=1",
            svc=True,
        ) or []
    except Exception:
        rows = []
    if not rows:
        return ""
    return str(rows[0].get("created_at") or "")


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def _build_message(agg: dict[str, dict[str, int]]) -> tuple[str, dict]:
    task_status = autonomy_status() if callable(autonomy_status) else {}
    research_status = research_autonomy_status() if callable(research_autonomy_status) else {}
    code_status = code_autonomy_status() if callable(code_autonomy_status) else {}
    integration_status = integration_autonomy_status() if callable(integration_autonomy_status) else {}
    evolution_status = evolution_autonomy_status() if callable(evolution_autonomy_status) else {}
    proposal_status = proposal_router_status(limit=5) or {}

    task = agg.get("task_autonomy", {})
    research = agg.get("research_autonomy", {})
    code = agg.get("code_autonomy", {})
    integration = agg.get("integration_autonomy", {})
    evolution = agg.get("evolution_autonomy", {})

    task_line = (
        f"Processed: {_safe_int(task.get('processed'))} | Completed: {_safe_int(task.get('completed'))} | "
        f"Deferred: {_safe_int(task.get('deferred'))} | Failed: {_safe_int(task.get('failed'))} | "
        f"Blocked: {_safe_int(task.get('blocked'))} | Errors: {_safe_int(task.get('errors'))}"
    )
    task_queue = (
        f"Current queue: pending {task_status.get('pending', 0)} | in_progress {task_status.get('in_progress', 0)}"
    )

    research_line = (
        f"Processed: {_safe_int(research.get('processed'))} | Completed: {_safe_int(research.get('completed'))} | "
        f"Failed: {_safe_int(research.get('failed'))} | Skipped: {_safe_int(research.get('skipped'))}"
    )
    research_queue = (
        f"Pending research tasks: {research_status.get('pending', 0)} | Follow-up evolutions: {research_status.get('follow_up_queued', 0)}"
    )

    code_line = (
        f"Processed: {_safe_int(code.get('processed'))} | Proposed: {_safe_int(code.get('proposed'))} | "
        f"Duplicates: {_safe_int(code.get('duplicates'))} | Deferred: {_safe_int(code.get('deferred'))} | "
        f"Failures: {_safe_int(code.get('failures'))}"
    )
    code_queue = (
        f"Pending code tasks: {code_status.get('pending_code_tasks', 0)} | Pending review proposals: {code_status.get('pending_review_proposals', 0)}"
    )

    integration_line = (
        f"Processed: {_safe_int(integration.get('processed'))} | Proposed: {_safe_int(integration.get('proposed'))} | "
        f"Duplicates: {_safe_int(integration.get('duplicates'))} | Deferred: {_safe_int(integration.get('deferred'))} | "
        f"Failures: {_safe_int(integration.get('failures'))}"
    )
    integration_queue = (
        f"Pending integration tasks: {integration_status.get('pending_integration_tasks', 0)} | Pending review proposals: {integration_status.get('pending_review_proposals', 0)}"
    )

    evolution_line = (
        f"Processed: {_safe_int(evolution.get('processed'))} | Created: {_safe_int(evolution.get('queued'))} | "
        f"Duplicates: {_safe_int(evolution.get('duplicates'))} | Failures: {_safe_int(evolution.get('failures'))}"
    )
    evolution_queue = (
        f"Skipped total: {_safe_int(evolution.get('skipped'))} | Pending evolutions remaining: {evolution_status.get('pending_evolutions', 0)}"
    )
    evolution_task_queue = f"Task queue: pending {evolution_status.get('pending_improvement_tasks', 0)}"

    approved = _count_rows(
        "evolution_queue",
        f"select=id&status=eq.applied&updated_at=gte.{_window_start(AUTONOMY_DIGEST_WINDOW_HOURS)}",
    )
    rejected = _count_rows(
        "evolution_queue",
        f"select=id&status=eq.rejected&updated_at=gte.{_window_start(AUTONOMY_DIGEST_WINDOW_HOURS)}",
    )
    owner_in_queue = int(proposal_status.get("pending_owner_only", 0) or 0)
    activity_total = (
        sum(_safe_int(v) for v in task.values())
        + sum(_safe_int(v) for v in research.values())
        + sum(_safe_int(v) for v in code.values())
        + sum(_safe_int(v) for v in integration.values())
        + sum(_safe_int(v) for v in evolution.values())
        + _safe_int(task_status.get("pending", 0))
        + _safe_int(task_status.get("in_progress", 0))
        + _safe_int(research_status.get("pending", 0))
        + _safe_int(research_status.get("follow_up_queued", 0))
        + _safe_int(code_status.get("pending_code_tasks", 0))
        + _safe_int(code_status.get("pending_review_proposals", 0))
        + _safe_int(integration_status.get("pending_integration_tasks", 0))
        + _safe_int(integration_status.get("pending_review_proposals", 0))
        + _safe_int(evolution_status.get("pending_evolutions", 0))
        + _safe_int(evolution_status.get("pending_improvement_tasks", 0))
        + approved
        + rejected
        + owner_in_queue
    )

    message = "\n".join([
        "AUTONOMY WORKER SUMMARY:",
        "",
        "TASK AUTONOMY",
        "Cycle summary",
        "Window: 24-Hours",
        task_line,
        task_queue,
        "",
        "RESEARCH AUTONOMY CYCLE",
        "Window: 24-Hours",
        research_line,
        research_queue,
        "",
        "CODE AUTONOMY CYCLE",
        "Window: 24-Hours",
        code_line,
        code_queue,
        "",
        "INTEGRATION AUTONOMY CYCLE",
        "Window: 24-Hours",
        integration_line,
        integration_queue,
        "",
        "EVOLUTION AUTONOMY",
        "Cycle summary",
        "Window: 24-Hours",
        evolution_line,
        evolution_queue,
        evolution_task_queue,
        "",
        "OWNER_REVIEW TASK SUMMARY",
        f"In-Queue: {owner_in_queue} | Approved: {approved} | Rejected: {rejected}",
    ])
    summary = {
        "window_hours": AUTONOMY_DIGEST_WINDOW_HOURS,
        "activity_total": activity_total,
        "task": {**task, **{k: _safe_int(v) for k, v in task.items()}},
        "research": {**research, **{k: _safe_int(v) for k, v in research.items()}},
        "code": {**code, **{k: _safe_int(v) for k, v in code.items()}},
        "integration": {**integration, **{k: _safe_int(v) for k, v in integration.items()}},
        "evolution": {**evolution, **{k: _safe_int(v) for k, v in evolution.items()}},
        "owner_review": {
            "in_queue": owner_in_queue,
            "approved": approved,
            "rejected": rejected,
        },
    }
    return message, summary


def run_autonomy_digest(force: bool = False) -> dict:
    if not AUTONOMY_DIGEST_ENABLED:
        return {"ok": False, "enabled": False, "message": "CORE_AUTONOMY_DIGEST_ENABLED=false"}

    with _lock:
        if _state["running"]:
            return {"ok": False, "busy": True, "message": "autonomy digest already running"}
        _state["running"] = True

    try:
        last_digest_at = _current_last_digest_at()
        last_dt = _parse_iso(last_digest_at)
        now = datetime.utcnow()
        due = force or not last_dt or (now - last_dt).total_seconds() >= AUTONOMY_DIGEST_INTERVAL_S
        if not due:
            remaining = max(0, AUTONOMY_DIGEST_INTERVAL_S - int((now - last_dt).total_seconds())) if last_dt else AUTONOMY_DIGEST_INTERVAL_S
            return {
                "ok": True,
                "enabled": True,
                "sent": False,
                "due": False,
                "next_check_in": remaining,
                "last_digest_at": last_digest_at,
            }

        agg = _aggregate_sessions()
        message, summary = _build_message(agg)
        if not force and _safe_int(summary.get("activity_total")) <= 0:
            checked_at = _utcnow()
            _save_local_digest_state({
                "last_checked_at": checked_at,
                "last_digest_at": checked_at,
                "last_empty_at": checked_at,
                "last_activity_total": 0,
            })
            _state["last_run_at"] = checked_at
            _state["last_error"] = ""
            _state["last_summary"] = summary
            return {
                "ok": True,
                "enabled": True,
                "sent": False,
                "due": False,
                "reason": "no_activity",
                "last_digest_at": checked_at,
                "summary": summary,
            }
        notify(message)
        try:
            sb_post(
                "sessions",
                {
                    "summary": f"[state_update] autonomy_digest_last_run: {_utcnow()}",
                    "actions": [json.dumps(summary, default=str)],
                    "interface": "autonomy_digest",
                },
            )
        except Exception:
            pass
        finished_at = _utcnow()
        _save_local_digest_state({
            "last_checked_at": finished_at,
            "last_digest_at": finished_at,
            "last_empty_at": "",
            "last_activity_total": _safe_int(summary.get("activity_total")),
            "summary": summary,
        })
        _state["last_run_at"] = finished_at
        _state["last_error"] = ""
        _state["last_summary"] = summary
        return {
            "ok": True,
            "enabled": True,
            "sent": True,
            "finished_at": finished_at,
            "last_digest_at": last_digest_at,
            "summary": summary,
        }
    except Exception as e:
        _state["last_error"] = str(e)
        return {"ok": False, "enabled": True, "error": str(e)}
    finally:
        with _lock:
            _state["running"] = False


def autonomy_digest_loop() -> None:
    # Check frequently, but only emit a digest once per configured interval.
    while AUTONOMY_DIGEST_ENABLED:
        try:
            result = run_autonomy_digest(force=False)
            if result.get("busy"):
                time.sleep(min(60, AUTONOMY_DIGEST_CHECK_S))
            else:
                time.sleep(AUTONOMY_DIGEST_CHECK_S)
        except Exception as e:
            _state["last_error"] = str(e)
            time.sleep(min(300, AUTONOMY_DIGEST_CHECK_S))


def autonomy_digest_status() -> dict:
    return {
        "ok": True,
        "enabled": AUTONOMY_DIGEST_ENABLED,
        "running": _state["running"],
        "interval_seconds": AUTONOMY_DIGEST_INTERVAL_S,
        "check_seconds": AUTONOMY_DIGEST_CHECK_S,
        "window_hours": AUTONOMY_DIGEST_WINDOW_HOURS,
        "last_run_at": _state["last_run_at"],
        "last_error": _state["last_error"],
        "last_summary": _state["last_summary"],
        "last_digest_at": _current_last_digest_at(),
    }
